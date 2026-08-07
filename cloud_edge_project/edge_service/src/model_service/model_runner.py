# -*- coding: utf-8 -*-
"""Qwen 模型运行器（WSL 侧，torch/transformers 惰性导入）。

职责：加载模型、GPU 预热、串行 generate()、解析校验输出、返回结果。

关键正确性约束：
- 服务侧不得等待推理锁形成第二条隐式队列：模型空闲接受，忙则立即返回
  MODEL_BUSY（非阻塞 try-acquire），请求剩余时间不足立即拒绝；
- 任何时刻只有一个 generate()（推理锁）；HTTP 超时后推理可能仍在执行，
  锁保证不并发进入 generate()，但永久卡死当前阶段无法处理（文档写明限制）；
- 严格输出校验：confidence 必须为有限数值且 0<=c<=1，null/NaN/Inf/bool 全非法，
  不把 null 静默转换为 0.0；
- readiness 要求至少一次完整合法 JSON（加载→完整推理→JSON 解析→三字段合法），
  8 token 短推理只作 GPU 预热，不作可用性检查。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from model_input_contract import (
    ModelInputValidationError,
    model_input_probe,
    validate_model_input,
)

from .output_validator import validate_model_output
from .prompt import build_prompt

# 与 edge_model.contracts.HTTP_ERROR_* 对应（避免跨包循环导入，这里直接定义常量）
ERR_BUSY = "MODEL_BUSY"
ERR_UNAVAILABLE = "MODEL_UNAVAILABLE"
ERR_INFERENCE_FAILED = "MODEL_INFERENCE_FAILED"
ERR_INFERENCE_TIMEOUT = "MODEL_INFERENCE_TIMEOUT"
ERR_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
ERR_INPUT_INVALID = "MODEL_INPUT_INVALID"


def _bearing_probe() -> dict:
    """完整特征探针：能产生合法 3 字段 JSON 的轴承感知输入。"""
    return model_input_probe()


class ModelRunner:
    def __init__(self, model_path: str, model_version: str = "qwen2.5-1.5b-instruct/phase1",
                 dtype: str = "bfloat16", device: str = "auto",
                 max_new_tokens: int = 64, streamer_timeout_s: float = 30.0,
                 low_cpu_mem_usage: bool = True, gpu_warmup_calls: int = 2):
        self.model_path = model_path
        self.model_version = model_version
        self.max_new_tokens = max_new_tokens
        self.streamer_timeout_s = streamer_timeout_s
        self._infer_lock = threading.Lock()  # 任何时刻只有一个 generate
        self._ready = False
        self._load_error: Optional[str] = None

        self._torch, self._AutoModelForCausalLM, self._AutoTokenizer, self._TextIteratorStreamer = self._import_runtime()
        self.tokenizer, self.model = self._load(dtype, device, low_cpu_mem_usage)
        self.pad_token_id = (self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None
                             else self.tokenizer.eos_token_id)
        # GPU 预热（8 token 短推理）+ 完整可用性检查
        self._gpu_warmup(gpu_warmup_calls)
        self._full_availability_check()
        self._ready = True

    @staticmethod
    def _import_runtime():
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer
        return torch, AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer

    def _load(self, dtype, device, low_cpu_mem_usage):
        try:
            # 启动前检查：模型路径与关键文件存在，失败停止启动
            from pathlib import Path
            model_dir = Path(self.model_path)
            if not model_dir.is_dir():
                raise FileNotFoundError("模型目录不存在: %s" % self.model_path)
            missing = [f for f in ("config.json", "tokenizer_config.json") if not (model_dir / f).exists()]
            if missing:
                raise FileNotFoundError("模型目录缺少关键文件: %s" % ", ".join(missing))
            tokenizer = self._AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)
            dtype_obj = getattr(self._torch, dtype) if isinstance(dtype, str) else dtype
            model = self._AutoModelForCausalLM.from_pretrained(
                self.model_path, dtype=dtype_obj, device_map=device,
                low_cpu_mem_usage=low_cpu_mem_usage, trust_remote_code=True)
            model.eval()
            return tokenizer, model
        except Exception as exc:  # noqa: BLE001
            self._load_error = "%s: %s" % (type(exc).__name__, exc)
            raise

    # ---- 预热与可用性 ----
    def _gpu_warmup(self, calls: int):
        """8 token 短推理：只作 GPU 预热，不代表模型可用。"""
        probe = _bearing_probe()
        saved = self.max_new_tokens
        self.max_new_tokens = 8
        try:
            for _ in range(calls):
                self.infer(probe)  # 预热期间无并发，非阻塞锁必然可用
        finally:
            self.max_new_tokens = saved

    def _full_availability_check(self):
        """完整可用性检查：一次完整推理必须产出合法 3 字段 JSON，否则启动失败。"""
        res = self.infer(_bearing_probe())
        if res.get("valid") is not True:
            self._load_error = "availability_check_failed: %s" % res.get("error")
            raise RuntimeError(self._load_error)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    # ---- 推理（HTTP 处理器调用） ----
    def infer(self, model_input: dict, request_id: Optional[str] = None,
              remaining_timeout_ms: Optional[float] = None) -> Dict:
        """执行推理。忙 → 立即 MODEL_BUSY；剩余时间不足 → 立即拒绝。"""
        t_start = time.monotonic()
        try:
            validate_model_input(model_input)
        except ModelInputValidationError as exc:
            return {"valid": False, "error": ERR_INPUT_INVALID,
                    "detail": str(exc), "request_id": request_id, "latency_ms": 0.0}
        # 剩余时间不足：响应赶不上客户端总截止时间，立即拒绝
        if remaining_timeout_ms is not None and remaining_timeout_ms <= 0:
            return {"valid": False, "error": ERR_INFERENCE_TIMEOUT,
                    "request_id": request_id, "latency_ms": 0.0}
        # 非阻塞获取：模型空闲才接受，忙则立即返回，不排队等锁
        if not self._infer_lock.acquire(blocking=False):
            return {"valid": False, "error": ERR_BUSY,
                    "request_id": request_id, "latency_ms": 0.0}
        try:
            result = self._infer_locked(model_input, request_id)
        except Exception as exc:  # noqa: BLE001
            result = {"valid": False, "error": ERR_INFERENCE_FAILED,
                      "detail": "%s: %s" % (type(exc).__name__, exc),
                      "request_id": request_id,
                      "latency_ms": round((time.monotonic() - t_start) * 1000.0, 2)}
        finally:
            self._infer_lock.release()
        return result

    def _infer_locked(self, model_input: dict, request_id: Optional[str]) -> Dict:
        torch = self._torch
        t_start = time.monotonic()
        prompt = build_prompt(self.tokenizer, model_input)
        try:
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": ERR_INFERENCE_FAILED,
                    "detail": "tokenize_error: %s" % exc,
                    "request_id": request_id,
                    "latency_ms": round((time.monotonic() - t_start) * 1000.0, 2)}

        streamer = self._TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True,
            timeout=self.streamer_timeout_s)
        gen_kwargs = {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs["attention_mask"],
            "max_new_tokens": self.max_new_tokens,
            "do_sample": False,
            "pad_token_id": self.pad_token_id,
            "streamer": streamer,
            "return_dict_in_generate": True,
        }
        holder: Dict = {}

        def _generate():
            try:
                with torch.inference_mode():
                    t0 = time.monotonic()
                    holder["out"] = self.model.generate(**gen_kwargs)
                    holder["latency_ms"] = (time.monotonic() - t0) * 1000.0
                holder["ok"] = True
            except Exception as exc:  # noqa: BLE001
                holder["ok"] = False
                holder["err"] = exc

        t_gen = threading.Thread(target=_generate, daemon=True)
        t_gen.start()
        chunks = []
        streamer_timeout = False
        try:
            for chunk in streamer:
                chunks.append(chunk)
        except StopIteration:
            streamer_timeout = True
        except Exception:  # noqa: BLE001
            pass
        t_gen.join(timeout=self.streamer_timeout_s + 5)
        latency_ms = (time.monotonic() - t_start) * 1000.0

        if t_gen.is_alive() or streamer_timeout:
            return {"valid": False, "error": ERR_INFERENCE_TIMEOUT,
                    "request_id": request_id, "latency_ms": round(latency_ms, 2)}

        if not holder.get("ok"):
            return {"valid": False, "error": ERR_INFERENCE_FAILED,
                    "detail": "%s: %s" % (type(holder.get("err")).__name__, holder.get("err")),
                    "request_id": request_id, "latency_ms": round(latency_ms, 2)}

        text = "".join(chunks).strip()
        validation = validate_model_output(text)
        if not validation["valid"]:
            return {"valid": False, "error": ERR_OUTPUT_INVALID,
                    "detail": ",".join(validation["errors"]),
                    "request_id": request_id, "latency_ms": round(latency_ms, 2),
                    "raw_text": text}

        parsed = validation["parsed"]
        # 严格校验已保证 confidence 是 0<=c<=1 的有限数值；不再做 null→0.0 转换
        return {
            "valid": True,
            "edge_result": parsed["edge_result"],
            "edge_risk_level": parsed["edge_risk_level"],
            "confidence": float(parsed["confidence"]),
            "model_version": self.model_version,
            "request_id": request_id,
            "raw_text": text,
            "latency_ms": round(holder.get("latency_ms") or latency_ms, 2),
        }
