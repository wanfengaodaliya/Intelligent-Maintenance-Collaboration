"""Bearing-specific long-term risk analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.device_health_analyzer import analyze_device_health


def analyze_bearing_risk(rows: list[dict[str, Any]], config: GlobalAnalysisConfig) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        bearing_id = row.get("bearing_id")
        if isinstance(bearing_id, str) and bearing_id:
            grouped[bearing_id].append(row)
    bearings = []
    for bearing_id in sorted(grouped):
        health_rows = [
            {"final_state": row.get("bearing_state"), "completed_at_ns": row.get("completed_at_ns", 0)}
            for row in grouped[bearing_id]
        ]
        metrics = analyze_device_health(health_rows, config)
        bearings.append({"bearing_id": bearing_id, **metrics})
    primary = next(
        iter(sorted(
            bearings,
            key=lambda item: (
                -(item["abnormal_rate"] if item["abnormal_rate"] is not None else -1),
                -(item["warning_rate"] if item["warning_rate"] is not None else -1),
                -(item["recent_risk_rate"] if item["recent_risk_rate"] is not None else -1),
                item["bearing_id"],
            ),
        )),
        None,
    )
    return {
        "status": "succeeded" if any(item["status"] == "succeeded" for item in bearings) else "insufficient_data",
        "primary_risk_bearing_id": primary["bearing_id"] if primary else None,
        "degrading_bearing_count": sum(item["trend"] == "degrading" for item in bearings),
        "multi_bearing_degradation": sum(item["trend"] == "degrading" for item in bearings) >= 2,
        "bearings": bearings,
    }
