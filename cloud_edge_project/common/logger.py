"""JSONL task trace storage and metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.config import PROJECT_ROOT, load_config
from common.schemas import validate_task_trace


def log_path(config: dict[str, Any] | None = None) -> Path:
    loaded = config or load_config()
    return PROJECT_ROOT / loaded["log"]["path"]


def append_task_trace(trace: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    validated = validate_task_trace(trace)
    path = log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(validated, ensure_ascii=False) + "\n")
    return {
        "packet_id": validated["packet_id"],
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
    if total == 0:
        return {
            "total_packets": 0,
            "success_rate": 0.0,
            "avg_total_latency_ms": 0.0,
            "cloud_call_ratio": 0.0,
            "edge_only_ratio": 0.0,
            "fallback_edge_ratio": 0.0,
            "abnormal_ratio": 0.0,
        }
    success = sum(1 for trace in traces if trace.get("success"))
    avg_latency = sum(float(trace.get("total_latency_ms", 0)) for trace in traces) / total
    cloud = sum(1 for trace in traces if trace.get("route") == "cloud")
    edge = sum(1 for trace in traces if trace.get("route") == "edge")
    fallback = sum(1 for trace in traces if trace.get("route") == "fallback_edge")
    abnormal = sum(1 for trace in traces if trace.get("final_label") == "abnormal")
    return {
        "total_packets": total,
        "success_rate": round(success / total, 4),
        "avg_total_latency_ms": round(avg_latency, 2),
        "cloud_call_ratio": round(cloud / total, 4),
        "edge_only_ratio": round(edge / total, 4),
        "fallback_edge_ratio": round(fallback / total, 4),
        "abnormal_ratio": round(abnormal / total, 4),
    }

