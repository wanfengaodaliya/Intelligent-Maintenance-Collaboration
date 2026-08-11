"""Mock edge bearing model for the first-stage demo."""
# 该模块提供轴承初检演示使用的模拟边缘模型。

from __future__ import annotations

from statistics import mean
from time import perf_counter
from typing import Any

from common.schemas import validate_edge_result, validate_sensor_packet


EDGE_NODE_ID = "edge_01"
MODEL_NAME = "edge_bearing_mock"


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
