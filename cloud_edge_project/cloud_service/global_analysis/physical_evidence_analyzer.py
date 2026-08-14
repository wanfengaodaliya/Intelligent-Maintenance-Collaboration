"""Metrics for P1 physical evidence, kept separate from diagnosis decisions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from cloud_service.global_analysis.common import rate


def analyze_physical_evidence(
    rows: list[dict[str, Any]], *, edge_summary_count: int, available: bool
) -> dict[str, Any]:
    if not available:
        return {"status": "not_available", "evidence_sample_count": 0}
    event_count = sum(row.get("sample_type") == "event" for row in rows)
    normal_count = sum(row.get("sample_type") == "periodic_normal" for row in rows)
    complete_count = sum(bool(row.get("complete")) for row in rows)
    window_ms = [row.get("actual_window_ms") for row in rows if isinstance(row.get("actual_window_ms"), int)]
    triggers = Counter(
        reason for row in rows for reason in row.get("trigger_reasons", []) if isinstance(reason, str)
    )
    limitations = Counter(
        item.get("code") for row in rows for item in row.get("limitations", [])
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    )
    rms_values = [
        value for row in rows for value in [((row.get("result") or {}).get("time_domain") or {}).get("rms")]
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    count = len(rows)
    return {
        "status": "succeeded" if count else "insufficient_data",
        "evidence_sample_count": count,
        "event_sample_count": event_count,
        "periodic_normal_sample_count": normal_count,
        "complete_sample_count": complete_count,
        "partial_sample_count": count - complete_count,
        "complete_sample_rate": rate(complete_count, count),
        "trigger_reason_distribution": dict(sorted(triggers.items())),
        "limitation_code_distribution": dict(sorted(limitations.items())),
        "mean_rms": sum(rms_values) / len(rms_values) if rms_values else None,
        "multi_scale": {
            "edge_feature_window_ms": 50,
            "edge_summary_count": edge_summary_count,
            "physical_evidence_sample_count": count,
            "physical_evidence_mean_window_ms": sum(window_ms) / len(window_ms) if window_ms else None,
        },
    }
