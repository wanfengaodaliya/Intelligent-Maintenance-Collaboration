"""Trend summaries for the latest contiguous cloud packet window."""

from __future__ import annotations

from math import isfinite
from typing import Any


_TREND_FIELDS = (
    ("vibration_rms", ("vibration", "rms")),
    ("vibration_kurtosis", ("vibration", "kurtosis")),
    ("temperature", ("temperature",)),
)


def summarize_trends(packets: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """Return min/max/mean and first-to-last per-second slopes."""

    summaries: dict[str, dict[str, float | None]] = {}
    for name, path in _TREND_FIELDS:
        values = [_numeric_value(packet, path) for packet in packets]
        if not values or any(value is None for value in values):
            summaries[name] = _empty_summary()
            continue
        numeric_values = [value for value in values if value is not None]
        first = numeric_values[0]
        last = numeric_values[-1]
        summaries[name] = {
            "min": min(numeric_values),
            "max": max(numeric_values),
            "mean": sum(numeric_values) / len(numeric_values),
            "first_to_last_slope_per_second": _slope_per_second(
                first, last, packets[0]["end_timestamp_ns"], packets[-1]["end_timestamp_ns"]
            ),
        }
    return summaries


def _numeric_value(packet: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = packet
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _slope_per_second(
    first: float, last: float, first_timestamp_ns: int, last_timestamp_ns: int
) -> float | None:
    elapsed_ns = last_timestamp_ns - first_timestamp_ns
    if elapsed_ns <= 0:
        return None
    return (last - first) * 1_000_000_000 / elapsed_ns


def _empty_summary() -> dict[str, float | None]:
    return {
        "min": None,
        "max": None,
        "mean": None,
        "first_to_last_slope_per_second": None,
    }
