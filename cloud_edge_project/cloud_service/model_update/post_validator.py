"""Post-deployment outcome classification from new global-analysis metrics."""

from __future__ import annotations

from typing import Any

from cloud_service.model_update.contracts import DEFAULT_CONFIG, ModelUpdateConfig


TARGET_METRICS = {
    "risk_underestimation": "risk_underestimation_rate",
    "risk_overestimation": "risk_overestimation_rate",
}


def select_post_validation_metrics(
    analysis_result: dict[str, Any],
    *,
    problem_context: dict[str, Any],
    minimum_sample_count: int,
) -> dict[str, Any]:
    """Select ready overall or exact-context packet metrics from global analysis."""

    packet = analysis_result.get("packet_diagnosis_analysis")
    if (
        not isinstance(packet, dict)
        or packet.get("status") != "succeeded"
        or not isinstance(packet.get("reviewed_packet_count"), int)
        or packet["reviewed_packet_count"] < minimum_sample_count
    ):
        raise ValueError("POST_ANALYSIS_NOT_READY")
    if not problem_context:
        return packet
    candidates = packet.get("condition_metrics")
    contextual = list(candidates) if isinstance(candidates, list) else []
    weakness = packet.get("condition_weakness")
    if isinstance(weakness, dict) and weakness.get("status") == "succeeded":
        contextual.append(weakness)
    for metrics in contextual:
        if not isinstance(metrics, dict):
            continue
        if not all(metrics.get(key) == value for key, value in problem_context.items()):
            continue
        sample_count = metrics.get("reviewed_packet_count", metrics.get("sample_count"))
        if isinstance(sample_count, int) and sample_count >= minimum_sample_count:
            return metrics
    raise ValueError("POST_CONTEXT_METRICS_NOT_FOUND")


def validate_post_deployment(
    task: dict[str, Any],
    new_metrics: dict[str, Any],
    config: ModelUpdateConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    target_metric = TARGET_METRICS.get(task.get("problem_type"))
    if target_metric is None:
        raise ValueError("UNSUPPORTED_TARGET_PROBLEM")
    baseline = task.get("evidence_snapshot")
    if not isinstance(baseline, dict):
        raise ValueError("BASELINE_EVIDENCE_NOT_FOUND")
    old_target = _metric(baseline, target_metric)
    new_target = _metric(new_metrics, target_metric)
    improvement = old_target - new_target
    regressions = []
    for metric in (
        "cloud_correction_rate",
        "risk_underestimation_rate",
        "risk_overestimation_rate",
    ):
        if metric == target_metric or metric not in baseline or metric not in new_metrics:
            continue
        if _metric(new_metrics, metric) > _metric(baseline, metric) + config.post_regression_threshold:
            regressions.append(metric)
    if improvement > config.post_improvement_threshold + 1e-12:
        outcome = "partial_improvement" if regressions else "succeeded"
    else:
        outcome = "ineffective"
    return {
        "outcome": outcome,
        "target_metric": target_metric,
        "baseline_target_value": old_target,
        "post_target_value": new_target,
        "target_improvement": improvement,
        "regressed_metrics": regressions,
        "rollback_recommended": (
            new_target > old_target + config.post_regression_threshold
        ),
    }


def _metric(values: dict[str, Any], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"POST_VALIDATION_METRIC_REQUIRED:{key}")
    return float(value)
