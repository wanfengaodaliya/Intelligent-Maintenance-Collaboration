"""JSONL task trace storage and metrics."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from common.config import PROJECT_ROOT, load_config
from common.schemas import validate_task_log, validate_task_trace


def log_path(config: dict[str, Any] | None = None) -> Path:
    loaded = config or load_config()
    return PROJECT_ROOT / loaded["log"]["path"]


def append_task_trace(trace: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    if "packet_id" in trace:
        validated = validate_task_trace(trace)
        identifier = "packet_id"
    else:
        validated = validate_task_log(trace)
        identifier = "task_id"
    path = log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(validated, ensure_ascii=False) + "\n")
    return {
        identifier: validated[identifier],
        "saved": True,
        "log_path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
    }


def read_task_traces(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    path = log_path(config)
    if not path.exists():
        return []
    traces: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        traces.append(json.loads(line))
    return traces


def compute_metrics(traces: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(traces)
    success = sum(1 for trace in traces if trace.get("success"))
    latencies = sorted(float(trace.get("total_latency_ms", 0)) for trace in traces)
    avg_latency = sum(latencies) / total if total else 0.0
    p95_latency = latencies[math.ceil(0.95 * total) - 1] if total else 0.0
    cloud = sum(1 for trace in traces if trace.get("route") in {"cloud", "edge_cloud"})
    edge = sum(1 for trace in traces if trace.get("route") == "edge")
    fallback = [trace for trace in traces if trace.get("route") == "fallback_edge"]
    abnormal = sum(1 for trace in traces if trace.get("final_label") == "abnormal")
    conflicts = [trace for trace in traces if trace.get("has_conflict")]

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "total_packets": total,
        "success_rate": ratio(success, total),
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency_ms": p95_latency,
        "avg_total_latency_ms": round(avg_latency, 2),
        "cloud_call_ratio": ratio(cloud, total),
        "edge_only_ratio": ratio(edge, total),
        "weak_network_availability": ratio(
            sum(1 for trace in fallback if trace.get("success")), len(fallback)
        ),
        "conflict_rate": ratio(len(conflicts), total),
        "conflict_resolve_rate": ratio(
            sum(1 for trace in conflicts if trace.get("conflict_resolved") is True), len(conflicts)
        ),
        "fallback_edge_ratio": ratio(len(fallback), total),
        "abnormal_ratio": ratio(abnormal, total),
    }

