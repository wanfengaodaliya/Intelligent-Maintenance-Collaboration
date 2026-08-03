from __future__ import annotations

import numpy as np

from .contracts import AggregationError, RawWindow


def preprocess(window: RawWindow, config_version: str) -> RawWindow:
    if config_version != "cloud-preprocess-v1":
        raise AggregationError("PREPROCESSING_FAILED", "unknown preprocessing config")
    channels = {name: values.astype(np.float64, copy=True) - values.mean() for name, values in window.channels.items()}
    if any(not np.isfinite(values).all() for values in channels.values()):
        raise AggregationError("PREPROCESSING_FAILED", "preprocessed window contains non-finite values")
    return RawWindow(channels, window.relative_positions.copy(), window.packet_start_samples.copy())
