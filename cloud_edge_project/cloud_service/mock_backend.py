"""Deterministic cloud backend used for local development."""

from __future__ import annotations

from time import perf_counter
from typing import Any

CLOUD_NODE_ID = "cloud_1"
MODEL_NAME = "cloud_bearing_mock"


def infer_mock(perception_result: dict[str, Any]) -> dict[str, Any]:
    start = perf_counter()
    features = perception_result["cloud_recomputed_features"]
    vibration_rms = features["vibration"]["rms"]
    imbalance = features["current_relationship"]["current_imbalance_ratio"]
    label = "fault" if vibration_rms >= 1.0 or imbalance >= 0.1 else "normal"

    if label == "fault":
        confidence = 0.93
        risk_level = "high"
        recommended_action = "urgent_bearing_attention"
        description = "bearing anomaly risk is high; schedule inspection"
    else:
        confidence = 0.91
        risk_level = "low"
        recommended_action = "record_only"
        description = "bearing state is normal; keep monitoring"

    elapsed_ms = max((perf_counter() - start) * 1000, 12.0)
    return {
        "analysis_scope": "bearing_packet_review",
        "device_id": perception_result["device_id"],
        "task_id": perception_result["task_id"],
        "bearing_id": perception_result["bearing_id"],
        "packet_id": perception_result["packet_id"],
        "sender_id": perception_result["sender_id"],
        "cloud_node_id": CLOUD_NODE_ID,
        "model_name": MODEL_NAME,
        "label": label,
        "confidence": round(confidence, 2),
        "risk_level": risk_level,
        "cloud_latency_ms": round(elapsed_ms + 74.0, 2),
        "decision": {
            "recommended_action": recommended_action,
            "action": recommended_action,
            "description": description,
        },
    }
