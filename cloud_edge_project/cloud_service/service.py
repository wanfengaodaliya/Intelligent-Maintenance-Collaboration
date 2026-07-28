"""Validate cloud requests and dispatch them to the configured backend."""

from __future__ import annotations

from typing import Any

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.mock_backend import infer_mock
from cloud_service.perception.pipeline import run_perception
from cloud_service.storage.persistence import CloudReviewPersistence


def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
) -> dict[str, Any]:
    perception_result = run_perception(request)
    selected = settings or load_cloud_settings()
    if not perception_result["data_quality"]["valid"]:
        return {"success": True, "perception_result": perception_result, "review_result": None, "review_id": None}
    review_id = CloudReviewPersistence(selected.database_path).persist(request, perception_result)
    if selected.backend == "mock":
        review_result = infer_mock(perception_result)
    elif selected.backend == "vllm":
        from cloud_service.vllm_backend import infer_vllm

        review_result = infer_vllm(perception_result, selected)
    else:
        raise CloudServiceError(
            "MODEL_INFER_FAILED",
            f"unsupported cloud backend: {selected.backend}",
            500,
        )
    return {
        "success": True,
        "perception_result": perception_result,
        "review_result": review_result,
        "review_id": review_id,
    }
