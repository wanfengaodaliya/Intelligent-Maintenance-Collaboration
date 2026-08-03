"""Window quality checks and analysis-only preprocessing."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

from .config import AnalysisConfig
from .contracts import (
    EnhancedAnalysisError,
    LoadedWindow,
    PreparedWindow,
    REQUIRED_CHANNELS,
    limitation,
)


EPS = 1e-12


class WindowPreprocessor:
    def __init__(self, config: AnalysisConfig):
        self.config = config

    def prepare(self, window: LoadedWindow) -> PreparedWindow:
        x0: dict[str, np.ndarray] = {}
        x2: dict[str, np.ndarray] = {}
        x3: dict[str, np.ndarray] = {}
        available: dict[str, bool] = {}
        limitations: list[dict[str, str]] = list(window.limitations)

        for name in REQUIRED_CHANNELS:
            values = window.channels[name].astype(np.float64, copy=True)
            nonfinite_ratio = float(np.mean(~np.isfinite(values)))
            if nonfinite_ratio > self.config.max_nan_ratio:
                raise EnhancedAnalysisError(
                    "PREPROCESSING_FAILED",
                    f"{name} contains non-finite samples",
                    retryable=False,
                )
            x0[name] = values

        max_abs = max(float(np.abs(values).max()) for values in x0.values())
        if max_abs > 0.0:
            ratios = []
            for values in x0.values():
                if float(np.std(values)) <= EPS:
                    continue
                ratios.append(float(np.mean(np.abs(values) >= max_abs - 1e-9)))
            clipping_ratio = max(ratios) if ratios else 0.0
            if clipping_ratio >= self.config.clipping_ratio_fail:
                raise EnhancedAnalysisError(
                    "AGGREGATION_QUALITY_REJECTED",
                    "signal clipping ratio exceeds failure threshold",
                    retryable=False,
                )
            if clipping_ratio >= self.config.clipping_ratio_warn:
                limitations.append(
                    limitation("signal_clipping_suspected", "signal clipping is suspected")
                )

        sos = butter(
            self.config.filter_order,
            self.config.vibration_highpass_hz,
            btype="highpass",
            fs=self.config.vibration_sample_rate_hz,
            output="sos",
        )
        for name, values in x0.items():
            near_constant = float(np.std(values)) <= EPS
            filtered = sosfiltfilt(sos, values)
            if not np.all(np.isfinite(filtered)):
                raise EnhancedAnalysisError(
                    "PREPROCESSING_FAILED",
                    f"{name} filter produced non-finite values",
                    retryable=False,
                )
            normalized = (filtered - np.mean(filtered)) / max(float(np.std(filtered)), EPS)
            x2[name] = filtered
            x3[name] = normalized
            available[name] = not near_constant
            if near_constant:
                limitations.append(
                    limitation("near_constant_signal", f"{name} is near constant")
                )

        return PreparedWindow(
            x0=x0,
            x2=x2,
            x3=x3,
            available=available,
            limitations=limitations,
        )
