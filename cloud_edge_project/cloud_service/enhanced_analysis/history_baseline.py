"""Bootstrap and same-scale history baseline evidence."""

from __future__ import annotations

from typing import Any

from .config import AnalysisConfig
from .contracts import limitation


EPS = 1e-12
ENHANCED_FEATURES = (
    "vibration_rms",
    "vibration_kurtosis",
    "phase_current_1_rms_a",
    "phase_current_2_rms_a",
)
EDGE_FEATURES = (
    "vibration_rms",
    "vibration_kurtosis",
    "current_1_rms_a",
    "current_2_rms_a",
    "shaft_speed_rpm_mean",
    "bearing_radial_load_n_mean",
)


def analyze_history(
    *,
    edge_rows: list[dict[str, Any]],
    enhanced_rows: list[dict[str, Any]],
    current_speed_rpm: float | None,
    current_radial_load_n: float | None,
    current_time_domain: dict[str, Any],
    config: AnalysisConfig,
) -> dict[str, Any]:
    limitations: list[dict[str, str]] = []
    reference_ranges = _reference_ranges(edge_rows)
    edge_trend = _edge_trend(edge_rows)
    filtered = _same_scale_enhanced_rows(
        enhanced_rows, current_speed_rpm, current_radial_load_n
    )
    similar_cases = [
        {
            "review_id": row["review_id"],
            "created_at_ns": row["created_at_ns"],
            "hypothesis": _best_hypothesis(row),
        }
        for row in enhanced_rows
        if _best_hypothesis(row) is not None
    ][: config.similar_case_limit]

    if len(filtered) >= config.min_baseline_samples:
        scores = _robust_z_scores(current_time_domain, filtered)
        baseline_deviation_score = float(min(10.0, _mean_top3(scores)))
        trend = _enhanced_trend(filtered)
        baseline_source = "enhanced_analysis_result"
        comparable_features = list(ENHANCED_FEATURES)
        status = "available"
    else:
        if len(filtered) < config.min_baseline_samples:
            limitations.append(
                limitation(
                    "history_baseline_insufficient",
                    "same-scale enhanced-analysis history is insufficient",
                )
            )
        baseline_deviation_score = None
        trend = None
        comparable_features = []
        baseline_source = "bootstrap_reference" if edge_rows else "unavailable"
        status = "available" if edge_rows else "unavailable"

    return {
        "status": status,
        "baseline_source": baseline_source,
        "baseline_deviation_score": baseline_deviation_score,
        "trend": trend,
        "edge_summary_trend": edge_trend,
        "reference_ranges": reference_ranges,
        "comparable_feature_names": comparable_features,
        "similar_cases": similar_cases,
        "limitations": limitations,
    }


def _reference_ranges(edge_rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for feature in EDGE_FEATURES:
        values = [float(row[feature]) for row in edge_rows if row.get(feature) is not None]
        if not values:
            continue
        result[feature] = {
            "minimum": float(min(values)),
            "maximum": float(max(values)),
            "median": float(_median(values)),
        }
    return result


def _edge_trend(edge_rows: list[dict[str, Any]]) -> str | None:
    points = [
        (int(row["end_timestamp_ns"]), float(row["vibration_rms"]))
        for row in edge_rows
        if row.get("end_timestamp_ns") is not None and row.get("vibration_rms") is not None
    ]
    return _theil_sen_trend(points)


def _same_scale_enhanced_rows(
    rows: list[dict[str, Any]], current_speed_rpm: float | None, current_radial_load_n: float | None
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        conditions = (row.get("result") or {}).get("operating_conditions") or {}
        speed = conditions.get("speed_rpm")
        load = conditions.get("radial_load_n")
        if current_speed_rpm and speed is not None:
            if abs(float(speed) - current_speed_rpm) > 0.10 * current_speed_rpm:
                continue
        if current_radial_load_n and load is not None:
            if abs(float(load) - current_radial_load_n) > 0.15 * current_radial_load_n:
                continue
        result.append(row)
    return result


def _robust_z_scores(
    current_time_domain: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, float]:
    scores: dict[str, float] = {}
    values_by_feature = {
        "vibration_rms": [float(_td(item, "rms")) for item in _all_time_domain(rows)],
        "vibration_kurtosis": [float(_td(item, "kurtosis")) for item in _all_time_domain(rows)],
        "phase_current_1_rms_a": [float(_td(item, "rms")) for item in _all_time_domain(rows, "phase_current_1_A")],
        "phase_current_2_rms_a": [float(_td(item, "rms")) for item in _all_time_domain(rows, "phase_current_2_A")],
    }
    current = {
        "vibration_rms": _td(current_time_domain, "rms"),
        "vibration_kurtosis": _td(current_time_domain, "kurtosis"),
        "phase_current_1_rms_a": _td(current_time_domain.get("phase_current_1_A") or {}, "rms"),
        "phase_current_2_rms_a": _td(current_time_domain.get("phase_current_2_A") or {}, "rms"),
    }
    for feature, history_values in values_by_feature.items():
        value = current.get(feature)
        if value is None or not history_values:
            continue
        median = _median(history_values)
        mad = _median([abs(item - median) for item in history_values])
        robust_z = abs(value - median) / max(1.4826 * mad, EPS)
        scores[feature] = float(robust_z)
    return scores


def _all_time_domain(rows: list[dict[str, Any]], channel: str = "vibration") -> list[dict[str, Any]]:
    result = []
    for row in rows:
        time_domain = ((row.get("result") or {}).get("signal_evidence") or {}).get("time_domain") or {}
        item = time_domain.get(channel)
        if item is not None:
            result.append(item)
    return result


def _td(item: dict[str, Any], name: str) -> float | None:
    value = item.get(name) if isinstance(item, dict) else None
    return float(value) if value is not None else None


def _mean_top3(scores: dict[str, float] | list[float]) -> float:
    if isinstance(scores, dict):
        values = sorted(scores.values(), reverse=True)[:3]
    else:
        values = sorted(scores, reverse=True)[:3]
    return float(sum(values) / len(values)) if values else 0.0


def _enhanced_trend(rows: list[dict[str, Any]]) -> str | None:
    points = []
    for row in rows:
        time_domain = ((row.get("result") or {}).get("signal_evidence") or {}).get("time_domain") or {}
        rms = _td(time_domain.get("vibration") or {}, "rms")
        if rms is not None:
            points.append((int(row["created_at_ns"]), rms))
    return _theil_sen_trend(sorted(points)[-7:])


def _theil_sen_trend(points: list[tuple[int, float]]) -> str | None:
    if len(points) < 2:
        return None
    points = sorted(points)
    slopes = []
    for left in range(len(points)):
        for right in range(left + 1, len(points)):
            x1, y1 = points[left]
            x2, y2 = points[right]
            if x2 > x1:
                slopes.append((y2 - y1) / (x2 - x1))
    if not slopes:
        return None
    median_slope = _median(slopes)
    values = [point[1] for point in points]
    median_value = _median(values)
    mad = _median([abs(item - median_value) for item in values])
    threshold = max(EPS, 1.4826 * mad)
    if median_slope > threshold:
        return "rising"
    if median_slope < -threshold:
        return "falling"
    return "stable"


def _best_hypothesis(row: dict[str, Any]) -> str | None:
    matches = ((row.get("result") or {}).get("signal_evidence") or {}).get("bearing_matches") or []
    if not matches:
        return None
    return max(matches, key=lambda item: item.get("score") or 0.0).get("hypothesis")


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    size = len(ordered)
    if size == 0:
        return 0.0
    if size % 2:
        return float(ordered[size // 2])
    return float((ordered[size // 2 - 1] + ordered[size // 2]) / 2.0)
