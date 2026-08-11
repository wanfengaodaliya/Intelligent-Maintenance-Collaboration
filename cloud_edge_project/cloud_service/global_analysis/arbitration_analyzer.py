"""Device conflict and arbitration history analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cloud_service.global_analysis.common import rate
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def analyze_device_arbitration(
    device_rows: list[dict[str, Any]], arbitration_rows: list[dict[str, Any]], config: GlobalAnalysisConfig
) -> dict[str, Any]:
    task_count = len(device_rows)
    conflict_count = sum(bool(row.get("has_conflict")) for row in device_rows)
    arbitration_count = len(arbitration_rows)
    resolved_count = sum(row.get("status") == "resolved" for row in arbitration_rows)
    success = rate(resolved_count, arbitration_count)
    result: dict[str, Any] = {
        "status": "succeeded" if task_count else "insufficient_data",
        "device_task_count": task_count,
        "conflict_count": conflict_count,
        "conflict_rate": rate(conflict_count, task_count),
        "arbitration_count": arbitration_count,
        "resolved_count": resolved_count,
        "arbitration_success_rate": success,
        "conflict_target_met": rate(conflict_count, task_count) <= config.conflict_rate_target if task_count else None,
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
