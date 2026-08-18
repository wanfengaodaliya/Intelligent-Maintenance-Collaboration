"""Legacy synchronous demo contract retained for backward compatibility."""
# 该模块提供轴承初检演示使用的模拟边缘模型。

from __future__ import annotations

import os
import re
from statistics import mean
from time import perf_counter
from typing import Any, Mapping

from common.schemas import (
    require_confidence,
    require_field,
    require_number,
    validate_edge_result,
    validate_sensor_packet,
    validate_task_request,
)


_EDGE_NODE_ID_PATTERN = re.compile(r"^edge_\d{2,}$")


def load_edge_node_id(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    edge_node_id = env.get("EDGE_NODE_ID", "edge_01").strip()
    if not _EDGE_NODE_ID_PATTERN.fullmatch(edge_node_id):
        raise ValueError("EDGE_NODE_ID must match edge_<at least two digits>")
    return edge_node_id


EDGE_NODE_ID = load_edge_node_id()
MODEL_NAME = "edge_bearing_legacy_rule"
V01_MODEL_NAME = "edge_small_model"


def infer_edge_v01(task: dict[str, Any]) -> dict[str, Any]:
    """Produce the documented V0.1 EdgeResult from an industrial task."""

    validated = validate_task_request(task)
    data = validated["data"]

    if validated["scenario"] == "energy":
        # V0.1 deliberately accepts any non-empty energy business-data object.
        # With no domain optimizer in this service, return a neutral preliminary
        # result and request cloud review instead of inventing a dispatch policy.
        return {
            "task_id": validated["task_id"],
            "node_id": EDGE_NODE_ID,
            "model_name": V01_MODEL_NAME,
            "label": "normal",
            "confidence": 0.5,
            "risk_level": "low",
            "edge_latency_ms": 1.0,
            "need_cloud": True,
        }

    task_id = validated["task_id"]
    for field in ("temperature", "vibration", "current"):
        require_number(require_field(data, field, task_id), f"data.{field}", task_id)
    require_confidence(require_field(data, "load", task_id), "data.load", task_id)

    signals = sum((
        float(data["temperature"]) >= 70.0,
        float(data["vibration"]) >= 0.5,
        float(data["current"]) >= 10.0,
        float(data["load"]) >= 0.7,
    ))
    label = "abnormal" if signals >= 2 else "normal"
    confidence = round(0.6 + signals * 0.08 if label == "abnormal" else 0.9 - signals * 0.03, 2)
    risk_level = "high" if signals >= 3 else "medium" if label == "abnormal" else "low"
    return {
        "task_id": validated["task_id"],
        "node_id": EDGE_NODE_ID,
        "model_name": V01_MODEL_NAME,
        "label": label,
        "confidence": confidence,
        "risk_level": risk_level,
        "edge_latency_ms": 1.0,
        "need_cloud": label == "abnormal" and confidence < 0.9,
    }


def infer_edge(packet: dict[str, Any]) -> dict[str, Any]:
    start = perf_counter()
    validated = validate_sensor_packet(packet)
    data = validated["data"]
    vibration = [float(value) for value in data["vibration"]]
    mean_abs = mean(abs(value) for value in vibration)
    peak = max(abs(value) for value in vibration)

    anomaly_score = 0.0
    anomaly_score += min(mean_abs / 0.05, 1.0) * 0.35
    anomaly_score += min(peak / 0.12, 1.0) * 0.25
    anomaly_score += min(max((float(data["temperature"]) - 45.0) / 35.0, 0.0), 1.0) * 0.2
    anomaly_score += min(max((float(data["current"]) - 1.2) / 1.0, 0.0), 1.0) * 0.1
    anomaly_score += min(max((float(data["load"]) - 0.6) / 0.4, 0.0), 1.0) * 0.1

    label = "abnormal" if anomaly_score >= 0.45 else "normal"
    if label == "abnormal":
        confidence = 0.62 + min(anomaly_score, 1.0) * 0.22
    else:
        confidence = 0.86 + (1.0 - anomaly_score) * 0.1
    confidence = round(min(confidence, 0.97), 2)

    if label == "normal":
        risk_level = "low"
    elif confidence >= 0.8:
        risk_level = "high"
    else:
        risk_level = "medium"

    elapsed_ms = max((perf_counter() - start) * 1000, 1.0)
    result = {
        "packet_id": validated["packet_id"],
        "device_id": validated["device_id"],
        "edge_node_id": EDGE_NODE_ID,
        "model_name": MODEL_NAME,
        "label": label,
        "confidence": confidence,
        "risk_level": risk_level,
        "need_cloud": confidence < 0.8,
        "edge_latency_ms": round(elapsed_ms, 2),
    }
    return validate_edge_result(result, validated["packet_id"])
