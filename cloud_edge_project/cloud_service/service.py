"""Independent, structured cloud packet review."""

from __future__ import annotations

import time
from typing import Any

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.packet_diagnosis import (
    DiagnosisModel,
    PacketDiagnosis,
    RuleBasedDiagnosisModel,
)
from cloud_service.perception.pipeline import run_single_packet_perception
from cloud_service.storage.persistence import CloudReviewPersistence
from cloud_service.vllm_backend import infer_vllm


def infer_cloud(
    request: dict[str, Any],
    settings: CloudSettings | None = None,
    *,
    diagnosis_model: DiagnosisModel | None = None,
) -> dict[str, Any]:
    """Review one high-rate packet without requesting or aggregating context."""

    selected = settings or load_cloud_settings()
    perception_result = run_single_packet_perception(request)
    if not perception_result["data_quality"]["valid"]:
        return {
            "success": True,
            "perception_result": perception_result,
            "cloud_packet_result": None,
            "review_id": None,
        }

    review_id = CloudReviewPersistence(selected.database_path).persist_packet(
        request, perception_result
    )
    if diagnosis_model is not None:
        model = diagnosis_model
        diagnosis = model.predict(perception_result["cloud_recomputed_features"])
        model_version = model.model_version
    elif selected.backend == "mock":
        model = RuleBasedDiagnosisModel()
        diagnosis = model.predict(perception_result["cloud_recomputed_features"])
        model_version = model.model_version
    elif selected.backend == "vllm":
        model_result = infer_vllm(perception_result, selected)
        diagnosis = PacketDiagnosis(
            label=model_result["label"],
            confidence=model_result["confidence"],
            risk_level=model_result["risk_level"],
            recommended_action=model_result["decision"]["recommended_action"],
        )
        model_version = model_result["model_name"]
    else:
        raise CloudServiceError(
            "INVALID_CLOUD_BACKEND",
            f"unsupported cloud backend: {selected.backend}",
            500,
        )
    packet_result = _cloud_packet_result(
        review_id, request, diagnosis, model_version
    )
    return {
        "success": True,
        "perception_result": perception_result,
        "cloud_packet_result": packet_result,
        "review_result": packet_result,
        "review_id": review_id,
    }


def _cloud_packet_result(
    review_id: str,
    request: dict[str, Any],
    diagnosis: PacketDiagnosis,
    model_version: str,
) -> dict[str, Any]:
    edge = request["edge_perception_result"]
    inference = edge.get("edge_inference") or {}
    edge_label = inference.get("edge_result") or inference.get("label")
    if edge_label == "fault":
        edge_label = "abnormal"
    return {
        "review_id": review_id,
        "device_id": edge["device_id"],
        "task_id": edge["task_id"],
        "bearing_id": edge["bearing_id"],
        "packet_id": edge["packet_id"],
        "edge_label": edge_label,
        "edge_confidence": inference.get("confidence"),
        "edge_model_version": edge.get("edge_model_version"),
        "cloud_label": diagnosis.label,
        "cloud_confidence": diagnosis.confidence,
        "cloud_model_version": model_version,
        "risk_level": diagnosis.risk_level,
        "recommended_action": diagnosis.recommended_action,
        "created_at_ns": time.time_ns(),
    }
