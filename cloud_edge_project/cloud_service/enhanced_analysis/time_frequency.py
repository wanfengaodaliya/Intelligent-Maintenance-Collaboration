"""Frame-RMS nonstationarity scoring."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import AnalysisConfig
from .contracts import limitation


EPS = 1e-12


def analyze_time_frequency(
    x0: np.ndarray, sample_rate_hz: int, config: AnalysisConfig
) -> dict[str, Any]:
    values = np.asarray(x0, dtype=np.float64)
    frame_length = round(sample_rate_hz * config.frame_seconds)
    hop = max(1, round(frame_length * (1.0 - config.frame_overlap_ratio)))
    if frame_length < 2 or values.size < 2 * frame_length:
        return {
            "frame_count": 0,
            "time_step_s": hop / sample_rate_hz,
            "frame_rms": {"mean": None, "std": None, "cv": None},
            "energy_mutation_frames": [],
            "nonstationarity_score": 0.0,
            "limitations": [
                limitation("insufficient_stft_frames", "window has fewer than two RMS frames")
            ],
        }

    frame_rms = []
    start = 0
    while start + frame_length <= values.size:
        frame = values[start : start + frame_length]
        frame_rms.append(float(np.sqrt(np.mean(frame * frame))))
        start += hop
    frame_rms = np.asarray(frame_rms, dtype=np.float64)
    mean_rms = float(np.mean(frame_rms))
    std_rms = float(np.std(frame_rms))
    cv = std_rms / max(mean_rms, EPS)
    changes = np.abs(np.diff(frame_rms))
    median_change = float(np.median(changes)) if changes.size else 0.0
    mad_change = (
        float(np.median(np.abs(changes - median_change))) if changes.size else 0.0
    )
    threshold = median_change + 3.0 * mad_change
    mutation_frames = (
        [int(index + 1) for index in np.where(changes > threshold)[0]]
        if changes.size
        else []
    )
    return {
        "frame_count": int(frame_rms.size),
        "time_step_s": hop / sample_rate_hz,
        "frame_rms": {
            "mean": mean_rms,
            "std": std_rms,
            "cv": cv,
        },
        "energy_mutation_frames": mutation_frames,
        "nonstationarity_score": float(min(1.0, cv)),
        "limitations": [],
    }
