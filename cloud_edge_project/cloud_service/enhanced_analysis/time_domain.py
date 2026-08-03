"""Deterministic time-domain evidence extraction."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import stats
from scipy.signal import find_peaks

from .contracts import PreparedWindow, REQUIRED_CHANNELS


EPS = 1e-12


def analyze_time_domain(
    prepared: PreparedWindow,
    start_timestamp_ns: int | None,
    packet_start_samples: tuple[int, ...],
    sample_rate_hz: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in REQUIRED_CHANNELS:
        if not prepared.available[name]:
            result[name] = {
                "mean": None,
                "std": None,
                "rms": None,
                "peak_abs": None,
                "peak_to_peak": None,
                "skewness": None,
                "kurtosis": None,
                "crest_factor": None,
                "impulse_count": None,
                "impacts": [],
            }
            continue
        x = prepared.x2[name]
        rms = float(np.sqrt(np.mean(x * x)))
        peak_abs = float(np.max(np.abs(x)))
        std = float(np.std(x, ddof=1))
        result[name] = {
            "mean": float(np.mean(x)),
            "std": std,
            "rms": rms,
            "peak_abs": peak_abs,
            "peak_to_peak": float(np.max(x) - np.min(x)),
            "skewness": float(stats.skew(x, bias=False)) if std > EPS else 0.0,
            "kurtosis": float(stats.kurtosis(x, fisher=False, bias=False)) if std > EPS else 0.0,
            "crest_factor": peak_abs / max(rms, EPS),
            "impulse_count": _impulse_count(x),
            "impacts": _impacts(x, start_timestamp_ns, packet_start_samples, sample_rate_hz),
        }
    return result


def _impulse_count(x: np.ndarray) -> int:
    values = np.abs(x)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    threshold = median + 4.0 * mad
    peaks, _ = find_peaks(values, height=threshold)
    return int(peaks.size)


def _impacts(
    x: np.ndarray,
    start_timestamp_ns: int | None,
    packet_start_samples: tuple[int, ...],
    sample_rate_hz: int,
) -> list[dict[str, Any]]:
    values = np.abs(x)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    threshold = median + 4.0 * mad
    peaks, _ = find_peaks(values, height=threshold)
    if peaks.size == 0:
        return []
    ordered = peaks[np.argsort(values[peaks])[::-1]][:5]
    boundaries = np.asarray(packet_start_samples, dtype=np.int64)
    packet_location_available = bool(boundaries.size)
    result = []
    for index in ordered:
        sample_index = int(index)
        packet_index = None
        if packet_location_available:
            positions = np.searchsorted(boundaries, sample_index, side="right") - 1
            if 0 <= positions < boundaries.size:
                packet_index = int(positions)
        result.append(
            {
                "sample_index": sample_index,
                "timestamp_ns": (
                    int(start_timestamp_ns + sample_index * 1_000_000_000 / sample_rate_hz)
                    if start_timestamp_ns is not None
                    else None
                ),
                "amplitude": float(x[sample_index]),
                "packet_index": packet_index,
                "packet_location_available": packet_location_available,
            }
        )
    return result
