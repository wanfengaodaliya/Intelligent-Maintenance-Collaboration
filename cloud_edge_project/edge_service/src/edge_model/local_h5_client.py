# -*- coding: utf-8 -*-
"""蒸馏 H5 正式边缘诊断客户端与原子热更新入口。"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .h5_probe import default_probe_dir, load_h5_probe_task
from .contracts import EDGE_RESULT_VALUES, EDGE_RISK_VALUES
from .model_client import ModelInferResult, ReadinessResult
from .version_store import (
    DEFAULT_MODEL_ROOT,
    read_active_version,
    set_active_version,
    version_dir,
)


H5_RUNTIME_MODEL_VERSION = "distilled_h5_kd_fold3_a9f20442"
MODEL_TYPE_DISTILLED_H5 = "distilled_h5"
H5_DIAGNOSIS_LABELS = {
    "healthy",
    "outer_ring_damage",
    "inner_ring_damage",
}


class H5ActivationError(RuntimeError):
    """候选 H5 加载、探针或原子切换失败。"""


@dataclass
class LocalH5ClientConfig:
    """本地 H5 运行配置；根目录和初始版本由启动层显式确定。"""

    readiness_probe_interval_s: float = 5.0
    model_root: Path = DEFAULT_MODEL_ROOT
    initial_version: str = H5_RUNTIME_MODEL_VERSION
    expected_version: Optional[str] = None
    probe_dir: Path = default_probe_dir()


@dataclass(frozen=True)
class _H5ModelHandle:
    model: Any
    version: str


class LocalH5ModelClient:
    """正式蒸馏 H5 推理客户端，提供请求级 handle 快照和原子版本切换。"""

    def __init__(
        self,
        cfg: Optional[LocalH5ClientConfig] = None,
        model_factory: Callable[..., Any] | None = None,
        clock=time.monotonic,
    ) -> None:
        self.cfg = cfg or LocalH5ClientConfig()
        self.cfg.model_root = Path(self.cfg.model_root)
        self.cfg.probe_dir = Path(self.cfg.probe_dir)
        self._clock = clock
        self._handle_lock = threading.Lock()
        self._activation_lock = threading.Lock()
        self._model_factory = model_factory or self._load_distilled_h5
        self._handle: Optional[_H5ModelHandle] = None

    @staticmethod
    def _load_distilled_h5(*, model_dir: Path, model_version: str) -> Any:
        try:
            from edge_diagnosis.distilled_h5_model import DistilledH5DiagnosticModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "distilled H5 runtime unavailable (torch/scipy missing?): %r" % (exc,)
            ) from exc
        return DistilledH5DiagnosticModel(
            model_dir=model_dir, model_version=model_version
        )

    def _load_handle(self, version: str) -> _H5ModelHandle:
        directory = version_dir(
            MODEL_TYPE_DISTILLED_H5, version, base=self.cfg.model_root
        )
        try:
            model = self._model_factory(model_dir=directory, model_version=version)
        except Exception as exc:  # noqa: BLE001
            raise H5ActivationError("MODEL_LOAD_FAILED: %s" % exc) from exc
        reported = getattr(model, "model_version", None)
        if reported != version:
            raise H5ActivationError(
                "MODEL_HANDLE_VERSION_MISMATCH: expected=%s got=%s"
                % (version, reported)
            )
        return _H5ModelHandle(model=model, version=version)

    def _ensure_handle(self) -> _H5ModelHandle:
        with self._handle_lock:
            if self._handle is not None:
                return self._handle
        with self._activation_lock:
            with self._handle_lock:
                if self._handle is not None:
                    return self._handle
            handle = self._load_handle(self.cfg.initial_version)
            with self._handle_lock:
                if self._handle is None:
                    self._handle = handle
                return self._handle

    def attach_model_for_test(self, model: Any) -> None:
        """注入替身模型（仅测试）。"""
        version = getattr(model, "model_version", self.cfg.initial_version)
        with self._handle_lock:
            self._handle = _H5ModelHandle(model=model, version=version)

    @property
    def current_version(self) -> str:
        with self._handle_lock:
            handle = self._handle
        return handle.version if handle is not None else self.cfg.initial_version

    @property
    def model_version(self) -> str:
        return self.current_version

    def readiness(self) -> ReadinessResult:
        try:
            handle = self._ensure_handle()
        except Exception as exc:  # noqa: BLE001
            return ReadinessResult(
                ok=False,
                model_version=self.cfg.initial_version,
                detail="local H5 load failed: %s" % exc,
            )
        mismatch = (
            self.cfg.expected_version is not None
            and handle.version != self.cfg.expected_version
        )
        detail = (
            "local distilled H5 ready (three-channel parallel)"
            if not mismatch
            else "model_version mismatch: expected=%s reported=%s"
            % (self.cfg.expected_version, handle.version)
        )
        return ReadinessResult(
            ok=not mismatch,
            model_version=handle.version,
            version_mismatch=mismatch,
            detail=detail,
        )

    def build_evidence(self, raw_packet: dict) -> dict:
        handle = self._ensure_handle()
        return handle.model.build_evidence(raw_packet)

    def infer_task(
        self,
        task,
        inference_timeout_ms: Optional[int] = None,
        cancel_event=None,
    ) -> ModelInferResult:
        del inference_timeout_ms
        t0 = self._clock()
        try:
            handle = self._ensure_handle()
            edge = handle.model.run(task, cancel_event=cancel_event)
            if edge.model_version != handle.version:
                raise H5ActivationError("MODEL_RESULT_VERSION_MISMATCH")
        except Exception:  # noqa: BLE001
            return ModelInferResult(
                success=False,
                timed_out=False,
                error="MODEL_INFERENCE_FAILED",
                latency_ms=(self._clock() - t0) * 1000.0,
                request_id=task.request_id,
            )
        return ModelInferResult(
            success=True,
            edge=edge,
            latency_ms=(self._clock() - t0) * 1000.0,
            request_id=task.request_id,
        )

    def activate_version(self, target_version: str) -> dict[str, str]:
        """探针通过后原子持久化指针并替换运行 handle。"""
        if not isinstance(target_version, str) or not target_version.strip():
            raise H5ActivationError("MODEL_TARGET_VERSION_INVALID")
        target_version = target_version.strip()
        if (
            self.cfg.expected_version is not None
            and target_version != self.cfg.expected_version
        ):
            raise H5ActivationError(
                "MODEL_UPDATE_PINNED: expected=%s target=%s"
                % (self.cfg.expected_version, target_version)
            )

        with self._activation_lock:
            candidate = self._load_handle(target_version)
            pipeline_version = getattr(
                candidate.model, "feature_pipeline_version", None
            )
            if not isinstance(pipeline_version, str) or not pipeline_version:
                raise H5ActivationError("MODEL_FEATURE_PIPELINE_VERSION_MISSING")
            probe_task = load_h5_probe_task(
                self.cfg.probe_dir,
                expected_feature_pipeline_version=pipeline_version,
            )
            try:
                probe_result = candidate.model.run(probe_task)
                self._validate_probe_result(probe_result, target_version)
            except Exception as exc:  # noqa: BLE001
                raise H5ActivationError("MODEL_PROBE_FAILED: %s" % exc) from exc

            with self._handle_lock:
                previous = self._handle
                previous_version = (
                    previous.version if previous is not None else self.cfg.initial_version
                )
                try:
                    set_active_version(
                        MODEL_TYPE_DISTILLED_H5,
                        target_version,
                        base=self.cfg.model_root,
                    )
                    pointer_version = read_active_version(
                        MODEL_TYPE_DISTILLED_H5, base=self.cfg.model_root
                    )
                    if pointer_version != target_version:
                        raise H5ActivationError(
                            "MODEL_ACTIVE_POINTER_VERSION_MISMATCH"
                        )
                    self._handle = candidate
                except Exception:
                    set_active_version(
                        MODEL_TYPE_DISTILLED_H5,
                        previous_version,
                        base=self.cfg.model_root,
                    )
                    raise

            if self.current_version != target_version:
                raise H5ActivationError("MODEL_RUNTIME_VERSION_MISMATCH")
            persisted = read_active_version(
                MODEL_TYPE_DISTILLED_H5, base=self.cfg.model_root
            )
            if persisted != target_version:
                raise H5ActivationError("MODEL_PERSISTED_VERSION_MISMATCH")
            return {
                "runtime_version": target_version,
                "active_pointer_version": persisted,
            }

    @staticmethod
    def _validate_probe_result(result: Any, target_version: str) -> None:
        if getattr(result, "model_version", None) != target_version:
            raise H5ActivationError("MODEL_PROBE_VERSION_MISMATCH")
        if getattr(result, "edge_result", None) not in EDGE_RESULT_VALUES:
            raise H5ActivationError("MODEL_PROBE_EDGE_RESULT_INVALID")
        if getattr(result, "edge_risk_level", None) not in EDGE_RISK_VALUES:
            raise H5ActivationError("MODEL_PROBE_RISK_LEVEL_INVALID")
        confidence = getattr(result, "confidence", None)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise H5ActivationError("MODEL_PROBE_CONFIDENCE_INVALID")
        diagnosis_label = getattr(result, "diagnosis_label", None)
        if diagnosis_label not in H5_DIAGNOSIS_LABELS:
            raise H5ActivationError("MODEL_PROBE_DIAGNOSIS_LABEL_INVALID")
        probabilities = getattr(result, "class_probabilities", None)
        if (
            not isinstance(probabilities, dict)
            or set(probabilities) != H5_DIAGNOSIS_LABELS
        ):
            raise H5ActivationError("MODEL_PROBE_PROBABILITIES_MISSING")
        values = list(probabilities.values())
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise H5ActivationError("MODEL_PROBE_PROBABILITIES_INVALID")
        if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-5):
            raise H5ActivationError("MODEL_PROBE_PROBABILITY_SUM_INVALID")
