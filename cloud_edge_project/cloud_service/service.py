"""Validate cloud requests and dispatch them to the configured backend."""

from __future__ import annotations

from typing import Any

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.mock_backend import infer_mock
from cloud_service.perception.pipeline import (
    run_perception,
    run_preliminary_perception,
)
from cloud_service.raw_context.coordinator import RawContextCoordinator
from cloud_service.raw_context.transport import RawContextTransport
from cloud_service.storage.persistence import CloudReviewPersistence


def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
    *,
    context_transport: RawContextTransport | None = None,
) -> dict[str, Any]:
    selected = settings or load_cloud_settings()
    if context_transport is not None:
        return _begin_context_review(
            request,
            selected,
            context_transport,
        )
    perception_result = run_perception(request)
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


def _begin_context_review(
    request: dict[str, Any],
    settings: CloudSettings,
    transport: RawContextTransport,
) -> dict[str, Any]:
    perception_result = run_preliminary_perception(request)
    if not perception_result["data_quality"]["valid"]:
        return {
            "success": True,
            "perception_result": perception_result,
            "review_result": None,
            "review_id": None,
            "raw_context_request": None,
        }
    raw = request["cloud_raw_packet"]
    review_id = CloudReviewPersistence(
        settings.database_path
    ).persist_preliminary(request, perception_result)
    context_request = RawContextCoordinator(
        settings.database_path,
        transport=transport,
    ).create_and_dispatch(
        review_id=review_id,
        task_id=raw["task_id"],
        sender_id=raw["sender_id"],
        anchor_packet_id=raw["packet_id"],
        anchor_sequence_number=raw["sequence_number"],
    )
    return {
        "success": True,
        "perception_result": perception_result,
        "review_result": None,
        "review_id": review_id,
        "raw_context_request": context_request,
    }
