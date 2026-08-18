"""Device-level history analysis."""

from __future__ import annotations

from typing import Any

from cloud_service.global_analysis.common import analysis_status, normalized_state, rate, severity_of
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def analyze_device_health(rows: list[dict[str, Any]], config: GlobalAnalysisConfig) -> dict[str, Any]:
    valid = [row for row in rows if normalized_state(row.get("final_state"))]
    valid.sort(key=lambda row: row.get("completed_at_ns", 0))
    count = len(valid)
    counts = {state: sum(normalized_state(row["final_state"]) == state for row in valid) for state in ("normal", "warning", "abnormal")}
    risks = counts["warning"] + counts["abnormal"]
    recent = valid[-min(5, count):]
    consecutive_risk = _trailing_count(valid, lambda row: severity_of(row["final_state"]) > 0)
    consecutive_abnormal = _trailing_count(valid, lambda row: normalized_state(row["final_state"]) == "abnormal")
    trend = _trend(valid, config.trend_threshold) if count >= config.min_device_task_count else "insufficient_data"
    return {
        "status": analysis_status(count, config.min_device_task_count),
        "task_count": count,
        "latest_state": normalized_state(valid[-1]["final_state"]) if valid else None,
        **{f"{state}_count": counts[state] for state in counts},
        **{f"{state}_rate": rate(counts[state], count) for state in counts},
        "risk_task_count": risks,
        "risk_task_rate": rate(risks, count),
        "recent_risk_rate": rate(sum(severity_of(row["final_state"]) > 0 for row in recent), len(recent)),
        "consecutive_risk_tasks": consecutive_risk,
        "consecutive_abnormal_tasks": consecutive_abnormal,
        "trend": trend,
    }


def _trailing_count(rows: list[dict[str, Any]], predicate) -> int:
    count = 0
    for row in reversed(rows):
        if not predicate(row):
            break
        count += 1
    return count


def _trend(rows: list[dict[str, Any]], threshold: float) -> str:
    split = len(rows) // 2
    older = [severity_of(row["final_state"]) for row in rows[:split]]
    recent = [severity_of(row["final_state"]) for row in rows[split:]]
    delta = sum(recent) / len(recent) - sum(older) / len(older)
    if delta >= threshold:
        return "degrading"
    if delta <= -threshold:
        return "improving"
    return "stable"
