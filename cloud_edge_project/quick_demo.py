"""Run the first-stage cloud-edge collaboration flow.

Default mode runs directly in-process and needs no HTTP dependencies. Use
`--http` after installing requirements and starting services.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from typing import Any

from cloud_service.model import infer_cloud
from common.config import load_config, service_url
from common.logger import append_task_trace, compute_metrics, read_task_traces
from common.schemas import compact_packet_for_scheduler
from edge_service.model import infer_edge
from scheduler.rule_scheduler import decide_schedule
from simulator.task_generator import generate_sensor_packet


def estimate_payload_size_kb(packet: dict[str, Any]) -> float:
    return round(len(json.dumps(packet, ensure_ascii=False).encode("utf-8")) / 1024, 2)


def build_schedule_request(packet: dict[str, Any], edge_result: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {
        "packet": compact_packet_for_scheduler(packet, estimate_payload_size_kb(packet)),
        "edge_result": {
            "label": edge_result["label"],
            "confidence": edge_result["confidence"],
            "risk_level": edge_result["risk_level"],
            "need_cloud": edge_result["need_cloud"],
            "edge_latency_ms": edge_result["edge_latency_ms"],
        },
        "network_state": config["demo"]["network_state"],
        "node_state": config["demo"]["node_state"],
    }


def build_task_trace(
    packet: dict[str, Any],
    edge_result: dict[str, Any],
    decision: dict[str, Any],
    cloud_result: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    network_latency_ms = config["demo"]["network_state"]["latency_ms"] if decision["route"] == "cloud" else 0
    if cloud_result:
        final_label = cloud_result["label"]
        final_confidence = cloud_result["confidence"]
        risk_level = cloud_result["risk_level"]
        cloud_label = cloud_result["label"]
        cloud_confidence = cloud_result["confidence"]
        cloud_latency_ms = cloud_result["cloud_latency_ms"]
    else:
        final_label = edge_result["label"]
        final_confidence = edge_result["confidence"]
        risk_level = edge_result["risk_level"]
        cloud_label = None
        cloud_confidence = None
        cloud_latency_ms = None

    total_latency = edge_result["edge_latency_ms"] + network_latency_ms + (cloud_latency_ms or 0)
    return {
        "packet_id": packet["packet_id"],
        "device_id": packet["device_id"],
        "sensor_id": packet["sensor_id"],
        "sequence_number": packet["sequence_number"],
        "data_type": packet["data"]["data_type"],
        "route": decision["route"],
        "edge_label": edge_result["label"],
        "edge_confidence": edge_result["confidence"],
        "cloud_label": cloud_label,
        "cloud_confidence": cloud_confidence,
        "final_label": final_label,
        "final_confidence": final_confidence,
        "risk_level": risk_level,
        "edge_latency_ms": edge_result["edge_latency_ms"],
        "network_latency_ms": network_latency_ms,
        "cloud_latency_ms": cloud_latency_ms,
        "total_latency_ms": round(total_latency, 2),
        "success": True,
        "error_code": None,
        "log_timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
    }


def run_direct(kind: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    packet = generate_sensor_packet(1, kind)
    edge_result = infer_edge(packet)
    schedule_request = build_schedule_request(packet, edge_result, config)
    decision = decide_schedule(schedule_request)
    cloud_result = None
    if decision["route"] == "cloud":
        cloud_result = infer_cloud(
            {
                "packet": packet,
                "edge_result": {
                    "label": edge_result["label"],
                    "confidence": edge_result["confidence"],
                    "risk_level": edge_result["risk_level"],
                },
            }
        )
    trace = build_task_trace(packet, edge_result, decision, cloud_result, config)
    saved = append_task_trace(trace, config)
    return packet, edge_result, decision, cloud_result, saved


def run_http(kind: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    import requests

    packet = generate_sensor_packet(1, kind)
    edge_response = requests.post(f"{service_url('edge', config)}/edge/infer", json=packet, timeout=5)
    edge_response.raise_for_status()
    edge_result = edge_response.json()

    schedule_request = build_schedule_request(packet, edge_result, config)
    schedule_response = requests.post(f"{service_url('scheduler', config)}/scheduler/decide", json=schedule_request, timeout=5)
    schedule_response.raise_for_status()
    decision = schedule_response.json()

    cloud_result = None
    if decision["route"] == "cloud":
        cloud_response = requests.post(
            f"{service_url('cloud', config)}/cloud/infer",
            json={
                "packet": packet,
                "edge_result": {
                    "label": edge_result["label"],
                    "confidence": edge_result["confidence"],
                    "risk_level": edge_result["risk_level"],
                },
            },
            timeout=180,
        )
        cloud_response.raise_for_status()
        cloud_result = cloud_response.json()

    trace = build_task_trace(packet, edge_result, decision, cloud_result, config)
    log_response = requests.post(f"{service_url('log', config)}/logs/task_trace", json=trace, timeout=5)
    log_response.raise_for_status()
    return packet, edge_result, decision, cloud_result, log_response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one end-to-end first-stage packet flow.")
    parser.add_argument("--kind", choices=["normal", "abnormal"], default="abnormal")
    parser.add_argument("--http", action="store_true", help="call running FastAPI services")
    args = parser.parse_args()
    config = load_config()
    packet, edge_result, decision, cloud_result, saved = run_http(args.kind, config) if args.http else run_direct(args.kind, config)
    metrics = compute_metrics(read_task_traces(config))

    print(f"packet_id: {packet['packet_id']}")
    print(f"edge_result: {edge_result['label']}, confidence={edge_result['confidence']}")
    print(f"route: {decision['route']}")
    if cloud_result:
        print(f"cloud_result: {cloud_result['label']}, confidence={cloud_result['confidence']}")
    print(f"task_trace saved: {saved['saved']} ({saved['log_path']})")
    print(f"metrics: {json.dumps(metrics, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
