"""Validate cloud requests and dispatch them to the configured backend."""

from __future__ import annotations

from typing import Any

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.mock_backend import infer_mock
from common.schemas import validate_cloud_request


def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
) -> dict[str, Any]:
    validated = validate_cloud_request(request)
    selected = settings or load_cloud_settings()
    if selected.backend == "mock":
        return infer_mock(validated)
    if selected.backend == "vllm":
        from cloud_service.vllm_backend import infer_vllm

        return infer_vllm(validated, selected)
    raise CloudServiceError(
        "MODEL_INFER_FAILED",
        f"unsupported cloud backend: {selected.backend}",
        500,
    )
