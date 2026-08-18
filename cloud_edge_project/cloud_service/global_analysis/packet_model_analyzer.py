"""Packet-level edge/cloud model performance analysis."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cloud_service.global_analysis.common import analysis_status, review_metrics
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def analyze_packet_model(
    rows: list[dict[str, Any]], config: GlobalAnalysisConfig, *, available: bool = True
) -> dict[str, Any]:
    metrics = review_metrics(rows, edge_field="edge_label", cloud_field="cloud_label")
    count = metrics["count"]
    result = {
        "status": analysis_status(count, config.min_packet_review_count, available=available),
        "metric_scope": "reviewed_packets_only",
        "reviewed_packet_count": count,
        "edge_cloud_agreement_count": metrics["agreement_count"],
        "edge_cloud_agreement_rate": metrics["agreement_rate"],
        "cloud_correction_count": metrics["correction_count"],
        "cloud_correction_rate": metrics["correction_rate"],
        "risk_underestimation_count": metrics["underestimation_count"],
        "risk_underestimation_rate": metrics["underestimation_rate"],
        "risk_overestimation_count": metrics["overestimation_count"],
        "risk_overestimation_rate": metrics["overestimation_rate"],
        "by_model_version": _by_model_version(rows),
        "condition_weakness": _condition_weakness(rows, config),
    }
    return result


def _by_model_version(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("edge_model_version") or "unknown"].append(row)
    results = []
    for version in sorted(grouped):
        metrics = review_metrics(grouped[version], edge_field="edge_label", cloud_field="cloud_label")
        results.append({
            "model_version": version, "reviewed_count": metrics["count"],
            "cloud_correction_rate": metrics["correction_rate"],
            "risk_underestimation_rate": metrics["underestimation_rate"],
            "risk_overestimation_rate": metrics["overestimation_rate"],
        })
    return results


def _condition_weakness(rows: list[dict[str, Any]], config: GlobalAnalysisConfig) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for condition, thresholds in config.condition_thresholds.items():
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            context = row.get("operating_context") or {}
            value = context.get(condition) if isinstance(context, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket = "low" if value < thresholds[0] else "medium" if value < thresholds[1] else "high"
                buckets[bucket].append(row)
        for bucket, bucket_rows in buckets.items():
            metrics = review_metrics(bucket_rows, edge_field="edge_label", cloud_field="cloud_label")
            if metrics["count"] >= config.min_condition_sample_count:
                candidates.append({
                    "condition": condition, "bucket": bucket, "reviewed_count": metrics["count"],
                    "cloud_correction_rate": metrics["correction_rate"],
                    "risk_underestimation_rate": metrics["underestimation_rate"],
                    "risk_overestimation_rate": metrics["overestimation_rate"],
                })
    if not candidates:
        return {"status": "insufficient_data"}
    best = max(candidates, key=lambda item: (item["cloud_correction_rate"], item["risk_underestimation_rate"], item["reviewed_count"], item["condition"], item["bucket"]))
    return {"status": "succeeded", **best}
