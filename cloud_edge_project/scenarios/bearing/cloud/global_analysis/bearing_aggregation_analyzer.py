"""Cloud-reviewed bearing aggregation performance analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cloud_service.global_analysis.common import analysis_status, review_metrics
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def analyze_bearing_aggregation(rows: list[dict[str, Any]], config: GlobalAnalysisConfig) -> dict[str, Any]:
    metrics = review_metrics(rows, edge_field="edge_state", cloud_field="cloud_state")
    count = metrics["count"]
    return {
        "status": analysis_status(count, config.min_bearing_review_count),
        "metric_scope": "reviewed_bearings_only",
        "bearing_review_count": count,
        "bearing_edge_cloud_agreement_count": metrics["agreement_count"],
        "bearing_edge_cloud_agreement_rate": metrics["agreement_rate"],
        "bearing_correction_count": metrics["correction_count"],
        "bearing_correction_rate": metrics["correction_rate"],
        "bearing_underestimation_count": metrics["underestimation_count"],
        "bearing_underestimation_rate": metrics["underestimation_rate"],
        "bearing_overestimation_count": metrics["overestimation_count"],
        "bearing_overestimation_rate": metrics["overestimation_rate"],
        "by_aggregation_version": _by_version(rows),
        "review_trigger_analysis": _trigger_analysis(rows),
    }


def _by_version(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("aggregation_version") or "unknown"].append(row)
    return [
        {
            "aggregation_version": version,
            "reviewed_count": (metrics := review_metrics(grouped[version], edge_field="edge_state", cloud_field="cloud_state"))["count"],
            "bearing_correction_rate": metrics["correction_rate"],
            "bearing_underestimation_rate": metrics["underestimation_rate"],
        }
        for version in sorted(grouped)
    ]


def _trigger_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    triggered = [row for row in rows if isinstance(row.get("review_trigger_reason"), str)]
    if not triggered:
        return {"status": "not_available"}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in triggered:
        grouped[row["review_trigger_reason"]].append(row)
    return {
        "status": "succeeded",
        "by_trigger_reason": [
            {"review_trigger_reason": reason, "trigger_count": metrics["count"], "cloud_correction_rate": metrics["correction_rate"]}
            for reason in sorted(grouped)
            for metrics in [review_metrics(grouped[reason], edge_field="edge_state", cloud_field="cloud_state")]
        ],
    }
