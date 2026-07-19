from __future__ import annotations

from simulator.task_generator import generate_sensor_packet


def make_valid_cloud_request(kind: str = "abnormal") -> dict:
    packet = generate_sensor_packet(1, kind)
    if kind == "abnormal":
        edge_result = {
            "label": "abnormal",
            "confidence": 0.72,
            "risk_level": "medium",
        }
    else:
        edge_result = {
            "label": "normal",
            "confidence": 0.91,
            "risk_level": "low",
        }
    return {"packet": packet, "edge_result": edge_result}
