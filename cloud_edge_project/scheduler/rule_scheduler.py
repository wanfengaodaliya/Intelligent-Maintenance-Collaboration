"""First-stage rule scheduler fixed by docs/api.md."""

from __future__ import annotations

from typing import Any

from common.schemas import validate_schedule_decision, validate_schedule_request


SCHEDULER_NODE_ID = "scheduler_1"


def decide_schedule(request: dict[str, Any]) -> dict[str, Any]:
    validated = validate_schedule_request(request)
    packet = validated["packet"]
    edge_result = validated["edge_result"]
    network = validated["network_state"]
    node = validated["node_state"]
    packet_id = packet["packet_id"]
    confidence = float(edge_result["confidence"])
    cloud_available = bool(network["cloud_available"])
    network_latency = float(network["latency_ms"])
    edge_latency = float(edge_result["edge_latency_ms"])
    queue_penalty = int(node["cloud_queue_length"]) * 5.0

    if confidence >= 0.8:
        decision = {
            "packet_id": packet_id,
            "route": "edge",
            "target_node": "edge_1",
            "upload_required": False,
            "reason": "edge confidence is high enough",
            "estimated_total_latency_ms": round(edge_latency, 2),
        }
    elif cloud_available:
        payload_transfer_ms = float(packet["payload_size_kb"]) / max(float(network["bandwidth_mbps"]), 0.1) * 8
        decision = {
            "packet_id": packet_id,
            "route": "cloud",
            "target_node": "cloud_1",
            "upload_required": True,
            "reason": "edge confidence is low and cloud is available",
            "estimated_total_latency_ms": round(edge_latency + network_latency + payload_transfer_ms + queue_penalty + 86.0, 2),
        }
    else:
        decision = {
            "packet_id": packet_id,
            "route": "fallback_edge",
            "target_node": "edge_1",
            "upload_required": False,
            "reason": "edge confidence is low but cloud is unavailable",
            "estimated_total_latency_ms": round(edge_latency, 2),
        }
    return validate_schedule_decision(decision, packet_id)

