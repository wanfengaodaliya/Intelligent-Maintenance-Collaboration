"""Adapters around the verified bearing H5 edge implementation."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from core.scenario_plugin import (
    EdgeInferenceMetadata,
    EdgeInferenceRuntime,
    EdgeInferenceRuntimeRequest,
)


LOGGER = logging.getLogger(__name__)
_TORCH_THREAD_CONFIG_LOCK = threading.Lock()
_APPLIED_TORCH_THREAD_CONFIG: tuple[int, int] | None = None


def configure_local_h5_torch_threads() -> dict[str, int]:
    """Limit PyTorch internal parallelism for the bearing H5 runtime."""
    global _APPLIED_TORCH_THREAD_CONFIG
    intraop = int(os.getenv("EDGE_TORCH_INTRAOP_THREADS", "1"))
    interop = int(os.getenv("EDGE_TORCH_INTEROP_THREADS", "1"))
    if intraop < 1 or interop < 1:
        raise ValueError(
            "EDGE_TORCH_INTRAOP_THREADS and EDGE_TORCH_INTEROP_THREADS must be >= 1"
        )
    requested = (intraop, interop)
    with _TORCH_THREAD_CONFIG_LOCK:
        if _APPLIED_TORCH_THREAD_CONFIG is not None:
            if requested != _APPLIED_TORCH_THREAD_CONFIG:
                raise ValueError(
                    "PyTorch thread configuration is already fixed for this process: "
                    f"intraop={_APPLIED_TORCH_THREAD_CONFIG[0]}, "
                    f"interop={_APPLIED_TORCH_THREAD_CONFIG[1]}"
                )
            return {"intraop": intraop, "interop": interop}

        import torch

        torch.set_num_threads(intraop)
        torch.set_num_interop_threads(interop)
        _APPLIED_TORCH_THREAD_CONFIG = requested
    return {"intraop": intraop, "interop": interop}


class BearingEdgeModelProvider:
    """Declare and construct the existing distilled-H5 runtime."""

    model_type = "distilled_h5"
    required_window_ms = 50
    pipeline_backend = "local_h5"
    deployment_status = "local_distilled_h5"

    @property
    def default_model_version(self) -> str:
        from scenarios.bearing.edge_inference.local_h5_client import (
            H5_RUNTIME_MODEL_VERSION,
        )

        return H5_RUNTIME_MODEL_VERSION

    def model_metadata(self) -> Mapping[str, Any]:
        return {
            "model_id": self.model_type,
            "model_version": self.default_model_version,
            "observation_window_ms": self.required_window_ms,
            "pipeline_backend": self.pipeline_backend,
        }

    def build_client(
        self,
        *,
        model_root: Path,
        bundled_model_root: Path,
        pinned_model_version: str | None,
    ) -> object:
        from scenarios.bearing.edge_inference.local_h5_client import (
            LocalH5ClientConfig,
            LocalH5ModelClient,
        )
        try:
            from edge_model.model_store import initialize_model_store
        except ModuleNotFoundError as exc:
            if exc.name != "edge_model":
                raise
            from edge_service.src.edge_model.model_store import initialize_model_store

        selection = initialize_model_store(
            model_root=model_root,
            bundled_model_root=bundled_model_root,
            baseline_version=self.default_model_version,
            pinned_version=pinned_model_version,
        )
        client = LocalH5ModelClient(
            LocalH5ClientConfig(
                model_root=selection.model_root,
                initial_version=selection.version,
                expected_version=pinned_model_version,
            )
        )
        readiness = client.readiness()
        if not readiness.ok:
            raise RuntimeError(readiness.detail)
        return client


class BearingEdgeInferenceProvider:
    """Expose bearing inference without changing the established algorithms."""

    def __init__(self, model_provider: BearingEdgeModelProvider | None = None) -> None:
        self.model_provider = model_provider or BearingEdgeModelProvider()

    @property
    def metadata(self) -> EdgeInferenceMetadata:
        return EdgeInferenceMetadata(
            backend_id=self.model_provider.pipeline_backend,
            default_model_version=self.model_provider.default_model_version,
            feature_extractor_version=self.model_provider.default_model_version,
            deployment_status=self.model_provider.deployment_status,
        )

    def build_runtime(
        self,
        request: EdgeInferenceRuntimeRequest,
    ) -> EdgeInferenceRuntime:
        if (
            request.lifecycle_enabled
            and request.observation_window_ms != self.model_provider.required_window_ms
        ):
            raise ValueError(
                "local_h5 requires v12.diagnosis_window_ms=50, got %d"
                % request.observation_window_ms
            )
        thread_config = configure_local_h5_torch_threads()
        LOGGER.info("configured local H5 PyTorch threads: %s", thread_config)
        client = self.model_provider.build_client(
            model_root=request.model_root,
            bundled_model_root=request.bundled_model_root,
            pinned_model_version=request.pinned_model_version,
        )
        return EdgeInferenceRuntime(
            pipeline_backend=self.model_provider.pipeline_backend,
            model_client=client,
            evidence_builder=client.build_evidence,
        )

    def infer_compatible(self, payload: Any) -> dict[str, Any]:
        from common.schemas import is_v01_task_request
        from edge_service.model import infer_edge, infer_edge_v01

        return infer_edge_v01(payload) if is_v01_task_request(payload) else infer_edge(payload)
