# -*- coding: utf-8 -*-
"""WSL 模型服务 HTTP 客户端（stdlib urllib，零额外依赖）。

正确性约束：
- 请求携带内部字段 request_id / remaining_timeout_ms（相对剩余时间，避免跨机
  单调时钟同步问题），这些字段不进入外部 EdgeResult；
- 服务端错误码（MODEL_BUSY / MODEL_UNAVAILABLE / MODEL_INPUT_INVALID /
  MODEL_INFERENCE_FAILED / MODEL_INFERENCE_TIMEOUT / MODEL_OUTPUT_INVALID）从响应体读取并原样映射到
  内部降级原因，不丢失；
- inference_timeout 在两层生效：HTTP 读取超时 + worker 侧 join 兜底。超时是
  逻辑超时，WSL 侧已开始的 generate 不会被中止，推理锁保证不并发。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import ModelClientConfig
from .contracts import EdgeResult


@dataclass
class ModelInferResult:
    success: bool
    timed_out: bool = False
    error: Optional[str] = None    # 内部错误码（MODEL_*），worker 据此映射 REASON_*
    edge: Optional[EdgeResult] = None
    latency_ms: Optional[float] = None
    raw_text: Optional[str] = None
    request_id: Optional[str] = None


@dataclass
class HealthResult:
    ok: bool
    detail: str = ""


@dataclass
class ReadinessResult:
    """阶段 7.2/7.4：结构化就绪结果（含模型版本与 pin 校验结论）。"""

    ok: bool
    model_version: Optional[str] = None
    version_mismatch: bool = False
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "model_version": self.model_version,
            "version_mismatch": self.version_mismatch,
            "detail": self.detail,
        }


class ModelClient:
    def __init__(self, cfg: ModelClientConfig, clock=time.monotonic):
        self.cfg = cfg
        self._clock = clock

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _request_json(self, path: str, payload: Optional[dict] = None,
                      read_timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """返回服务端 JSON 体。连接层错误抛异常由调用方处理；HTTP 错误码仍返回体内容。"""
        url = self._url(path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        timeout = read_timeout_s if read_timeout_s is not None else self.cfg.read_timeout_ms / 1000.0
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 服务端返回错误码时响应体仍携带 error 字段，必须读取
            body = exc.read().decode("utf-8", errors="replace")
        return json.loads(body)

    # ---- 健康检查 ----
    def health(self) -> HealthResult:
        try:
            body = self._request_json(self.cfg.health_path, read_timeout_s=self.cfg.connect_timeout_ms / 1000.0)
            return HealthResult(ok=body.get("status") == "ok", detail=str(body))
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, detail="%s: %s" % (type(exc).__name__, exc))

    def readiness(self) -> ReadinessResult:
        """阶段 7.2/7.4：就绪检查 + 版本对齐。

        - 服务端 ready 且（若配置了版本 pin）model_version 一致 → ok=True；
        - pin 不一致 → ok=False 且 version_mismatch=True（边缘不接新任务）。
        """
        try:
            body = self._request_json(
                self.cfg.readiness_path,
                read_timeout_s=self.cfg.connect_timeout_ms / 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            return ReadinessResult(ok=False, detail="%s: %s" % (type(exc).__name__, exc))
        version = body.get("model_version")
        version = version if isinstance(version, str) and version else None
        ready = body.get("ready") is True
        mismatch = (
            ready
            and self.cfg.expected_version is not None
            and version != self.cfg.expected_version
        )
        ok = ready and not mismatch
        detail = "model service ready" if ok else (
            "model_version mismatch: expected=%s reported=%s"
            % (self.cfg.expected_version, version) if mismatch else str(body)
        )
        return ReadinessResult(
            ok=ok, model_version=version, version_mismatch=mismatch, detail=detail,
        )

    # ---- 推理 ----
    def infer(self, model_input: dict, inference_timeout_ms: Optional[int] = None,
              request_id: Optional[str] = None,
              remaining_timeout_ms: Optional[float] = None) -> ModelInferResult:
        t0 = self._clock()
        read_timeout = (inference_timeout_ms or self.cfg.read_timeout_ms) / 1000.0
        payload: Dict[str, Any] = {"input": model_input}
        if request_id is not None:
            payload["request_id"] = request_id
        if remaining_timeout_ms is not None:
            payload["remaining_timeout_ms"] = remaining_timeout_ms
        try:
            body = self._request_json(self.cfg.infer_path, payload, read_timeout_s=read_timeout)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                return ModelInferResult(success=False, timed_out=True,
                                        latency_ms=(self._clock() - t0) * 1000.0,
                                        error="MODEL_INFERENCE_TIMEOUT")
            return ModelInferResult(success=False, timed_out=False,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_UNAVAILABLE")
        except Exception as exc:  # noqa: BLE001
            return ModelInferResult(success=False, timed_out=False,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_FAILED")
        latency_ms = (self._clock() - t0) * 1000.0

        response_request_id = body.get("request_id")
        if request_id is not None and response_request_id != request_id:
            return ModelInferResult(success=False, timed_out=False, latency_ms=latency_ms,
                                    error="MODEL_OUTPUT_INVALID",
                                    request_id=response_request_id)

        if body.get("valid") is not True:
            # 原样保留服务端错误码（MODEL_BUSY 等），供 worker 映射内部降级原因
            return ModelInferResult(success=False, timed_out=False, latency_ms=latency_ms,
                                    error=body.get("error") or "MODEL_INFERENCE_FAILED",
                                    request_id=response_request_id)
        edge = EdgeResult(
            edge_result=body["edge_result"],
            confidence=float(body["confidence"]),
            edge_risk_level=body["edge_risk_level"],
            model_version=body["model_version"],
        )
        return ModelInferResult(success=True, edge=edge, latency_ms=latency_ms,
                                raw_text=body.get("raw_text"), request_id=response_request_id)
