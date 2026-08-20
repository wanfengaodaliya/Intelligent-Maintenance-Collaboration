# -*- coding: utf-8 -*-
"""蒸馏模型 H5 本地推理客户端（正式边缘诊断路线：三通道并行 + 加权融合）。

以与 :class:`edge_model.model_client.ModelClient` 同形的接口接入
``EdgeModelPipeline`` 的 worker 路线，使 H5 与阶段 5/7 的可靠性设施
（有界队列、超时预算、熔断、就绪探针、队列观测）完全复用：

- ``infer_task(task, budget_ms)``：推理入口。H5 需要 raw packet（原始波形），
  因此不走 ``infer(perception, ...)`` 通道，由 ``InferenceWorker`` 通过
  ``infer_task`` 钩子传入完整任务；
- ``readiness()``：报告 H5 制品加载状态与版本（支持 EDGE_MODEL_VERSION pin）；
- ``build_evidence(raw_packet)``：单包感知证据构建，复用 H5 自带的证据合同。

本模块顶层不导入 torch：无 torch 的环境（如纯 HTTP 部署/部分测试环境）
可以安全导入；仅在选择 local_h5 后端并首次加载模型时才触发 torch 导入，
导入/制品失败会体现在 readiness（not ready），而不是启动崩溃。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .model_client import ModelInferResult, ReadinessResult
from .version_store import resolve_active_version

# 与 models/distilled_h5 制品内的 RUNTIME_MODEL_VERSION 保持一致
# （由测试守护两处常量同步；本常量必须在无 torch 环境可导入）。
H5_RUNTIME_MODEL_VERSION = "distilled_h5_kd_fold3_a9f20442"


@dataclass
class LocalH5ClientConfig:
    """本地 H5 客户端配置（与 ModelClientConfig 的探测/pin 字段对齐）。"""

    readiness_probe_interval_s: float = 5.0
    expected_version: Optional[str] = None


class _H5ModelHandle:
    """已加载的 DistilledH5DiagnosticModel 包装（仅在实际加载后存在）。"""

    def __init__(self, model: Any) -> None:
        self.model = model


class LocalH5ModelClient:
    """正式边缘诊断路线：蒸馏模型 H5 三通道并行本地推理。"""

    def __init__(self, cfg: Optional[LocalH5ClientConfig] = None,
                 model_factory=None, clock=time.monotonic) -> None:
        self.cfg = cfg or LocalH5ClientConfig()
        self._clock = clock
        self._lock = threading.Lock()
        # model_factory 仅测试注入用；生产路径懒加载真实 H5。
        self._model_factory = model_factory or self._load_distilled_h5
        self._handle: Optional[_H5ModelHandle] = None

    # ---- 模型加载 ----

    @staticmethod
    def _load_distilled_h5() -> Any:
        try:
            from edge_diagnosis.distilled_h5_model import DistilledH5DiagnosticModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "distilled H5 runtime unavailable (torch/scipy missing?): %r" % (exc,)
            ) from exc
        return DistilledH5DiagnosticModel()

    def _ensure_model(self) -> Any:
        if self._handle is not None:
            return self._handle.model
        with self._lock:
            if self._handle is not None:
                return self._handle.model
            # 加载失败不缓存错误：readiness 探针周期重试，制品修复后自动恢复。
            model = self._model_factory()
            self._handle = _H5ModelHandle(model)
            return model

    def attach_model_for_test(self, model: Any) -> None:
        """注入替身模型（仅测试）：绕过真实制品加载。"""
        with self._lock:
            self._handle = _H5ModelHandle(model)

    @property
    def model_version(self) -> str:
        # 激活版本由 EDGE_MODEL_VERSION / active_version.json 解析，缺省回退基线。
        return resolve_active_version(
            "distilled_h5", default_version=H5_RUNTIME_MODEL_VERSION
        )

    # ---- 就绪探针（pipeline 周期调用） ----

    def readiness(self) -> ReadinessResult:
        try:
            model = self._ensure_model()
            version = getattr(model, "model_version", H5_RUNTIME_MODEL_VERSION)
        except Exception as exc:  # noqa: BLE001
            return ReadinessResult(ok=False, model_version=H5_RUNTIME_MODEL_VERSION,
                                   detail="local H5 load failed: %s" % exc)
        mismatch = (
            self.cfg.expected_version is not None
            and version != self.cfg.expected_version
        )
        ok = not mismatch
        detail = (
            "local distilled H5 ready (three-channel parallel)"
            if ok else
            "model_version mismatch: expected=%s reported=%s"
            % (self.cfg.expected_version, version)
        )
        return ReadinessResult(ok=ok, model_version=version,
                               version_mismatch=mismatch, detail=detail)

    # ---- 证据构建（单包感知合同） ----

    def build_evidence(self, raw_packet: dict) -> dict:
        return self._ensure_model().build_evidence(raw_packet)

    # ---- 推理（worker 的 infer_task 钩子） ----

    def infer_task(self, task, inference_timeout_ms: Optional[int] = None,
                   cancel_event=None) -> ModelInferResult:
        t0 = self._clock()
        try:
            edge = self._ensure_model().run(task, cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001
            return ModelInferResult(
                success=False, timed_out=False,
                error="MODEL_INFERENCE_FAILED",
                latency_ms=(self._clock() - t0) * 1000.0,
                request_id=task.request_id,
            )
        return ModelInferResult(
            success=True, edge=edge,
            latency_ms=(self._clock() - t0) * 1000.0,
            request_id=task.request_id,
        )
