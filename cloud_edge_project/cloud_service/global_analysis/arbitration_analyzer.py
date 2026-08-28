"""Device conflict and arbitration history analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cloud_service.global_analysis.common import rate
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def analyze_device_arbitration(
    summary_rows: list[dict[str, Any]], arbitration_rows: list[dict[str, Any]], config: GlobalAnalysisConfig
) -> dict[str, Any]:
    eligible_rows = [
        row for row in summary_rows if not row.get("excluded_from_formal_metrics", False)
    ]
    complete_count = len(eligible_rows)
    incomplete_count = len(summary_rows) - complete_count
    conflict_count = sum(bool(row.get("has_conflict")) for row in eligible_rows)
    gaps = [int(row.get("max_cross_edge_grade_gap", 0)) for row in eligible_rows]
    level_gaps = [
        float(row["max_action_level_gap"])
        for row in eligible_rows
        if row.get("max_action_level_gap") is not None
    ]
    score_gaps = [
        float(row["max_action_score_gap"])
        for row in eligible_rows
        if row.get("max_action_score_gap") is not None
    ]
    new_semantics_rows = [
        row
        for row in eligible_rows
        if row.get("conflict_semantics") == "action_level_gap_v1"
    ]
    new_conflict_count = sum(
        bool(row.get("has_conflict")) for row in new_semantics_rows
    )
    semantics_counter: Counter = Counter(
        row.get("conflict_semantics") or "legacy" for row in eligible_rows
    )
    arbitration_count = len(arbitration_rows)
    resolved_count = sum(row.get("status") == "resolved" for row in arbitration_rows)
    success = rate(resolved_count, arbitration_count)
    result: dict[str, Any] = {
        "status": "succeeded" if complete_count else "insufficient_data",
        "device_task_count": complete_count,
        "complete_window_count": complete_count,
        "incomplete_window_count": incomplete_count,
        "conflict_count": conflict_count,
        "conflict_rate": rate(conflict_count, complete_count),
        "consistency_rate": rate(complete_count - conflict_count, complete_count),
        "average_decision_gap": (sum(gaps) / len(gaps)) if gaps else None,
        "max_decision_gap": max(gaps) if gaps else None,
        "average_action_level_gap": (
            (sum(level_gaps) / len(level_gaps)) if level_gaps else None
        ),
        "max_action_level_gap": max(level_gaps) if level_gaps else None,
        "average_action_score_gap": (
            (sum(score_gaps) / len(score_gaps)) if score_gaps else None
        ),
        "max_action_score_gap": max(score_gaps) if score_gaps else None,
        "action_level_conflict_rate": rate(new_conflict_count, len(new_semantics_rows)),
        "conflict_semantics_distribution": dict(sorted(semantics_counter.items())),
        "arbitration_count": arbitration_count,
        "resolved_count": resolved_count,
        "arbitration_success_rate": success,
        "arbitration_upload_success_rate": rate(arbitration_count, conflict_count),
        "conflict_target_met": rate(conflict_count, complete_count) <= config.conflict_rate_target if complete_count else None,
        "arbitration_target_met": success >= config.arbitration_success_target if success is not None else None,
    }
    distribution_names = {
        "dominant_bearing_id": "dominant_bearing_distribution",
        "final_action": "final_action_distribution",
        "resolution_method": "resolution_method_distribution",
        "rule_version": "rule_version_distribution",
    }
    for field, output_name in distribution_names.items():
        values = [row[field] for row in arbitration_rows if isinstance(row.get(field), str) and row[field]]
        result[output_name] = dict(sorted(Counter(values).items())) if values else None
    return result
