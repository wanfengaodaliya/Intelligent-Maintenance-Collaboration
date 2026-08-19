"""Small, side-effect-free helpers shared by global-analysis analyzers."""

from __future__ import annotations

from typing import Any


SEVERITY = {"normal": 0, "warning": 1, "fault": 2}


def normalized_state(value: object) -> str | None:
    # 兼容历史存量数据中的 "abnormal"，统一归一化为 "fault"。
    if value == "abnormal":
        value = "fault"
    return value if isinstance(value, str) and value in SEVERITY else None


def severity_of(value: object) -> int | None:
    state = normalized_state(value)
    return SEVERITY.get(state) if state else None


def rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def analysis_status(count: int, minimum: int, *, available: bool = True) -> str:
    if not available:
        return "not_available"
    return "succeeded" if count >= minimum else "insufficient_data"


def review_metrics(
    rows: list[dict[str, Any]], *, edge_field: str, cloud_field: str
) -> dict[str, int | float | None]:
    pairs = [
        row for row in rows
        if severity_of(row.get(edge_field)) is not None
        and severity_of(row.get(cloud_field)) is not None
    ]
    count = len(pairs)
    agreement = sum(row[edge_field] == row[cloud_field] for row in pairs)
    under = sum(severity_of(row[cloud_field]) > severity_of(row[edge_field]) for row in pairs)
    over = sum(severity_of(row[cloud_field]) < severity_of(row[edge_field]) for row in pairs)
    return {
        "count": count,
        "agreement_count": agreement,
        "correction_count": count - agreement,
        "agreement_rate": rate(agreement, count),
        "correction_rate": rate(count - agreement, count),
        "underestimation_count": under,
        "underestimation_rate": rate(under, count),
        "overestimation_count": over,
        "overestimation_rate": rate(over, count),
    }
