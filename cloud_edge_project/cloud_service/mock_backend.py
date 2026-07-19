"""Deterministic cloud backend used for local development."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from common.schemas import require_confidence


CLOUD_NODE_ID = "cloud_1"
MODEL_NAME = "cloud_bearing_mock"


def infer_mock(validated_request: dict[str, Any]) -> dict[str, Any]:
    start = perf_counter()
    packet = validated_request["packet"]
    edge_result = validated_request["edge_result"]
    edge_confidence = require_confidence(
        edge_result["confidence"],
        "edge_result.confidence",
        packet["packet_id"],
    )
    label = edge_result["label"]

    if label == "abnormal":
        confidence = max(0.9, min(edge_confidence + 0.21, 0.97))
        risk_level = "high"
        action = "send_alert"
        description = "bearing anomaly risk is high; schedule inspection"
    else:
        confidence = max(0.9, min(edge_confidence + 0.08, 0.97))
        risk_level = "low"
        action = "record_only"
        description = "bearing state is normal; keep monitoring"

    elapsed_ms = max((perf_counter() - start) * 1000, 12.0)
    return {
        "packet_id": packet["packet_id"],
        "device_id": packet["device_id"],
        "cloud_node_id": CLOUD_NODE_ID,
        "model_name": MODEL_NAME,
        "label": label,
        "confidence": round(confidence, 2),
        "risk_level": risk_level,
        "cloud_latency_ms": round(elapsed_ms + 74.0, 2),
        "decision": {
            "action": action,
            "description": description,
        },
    }
