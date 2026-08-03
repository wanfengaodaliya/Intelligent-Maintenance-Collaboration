"""Frozen analysis defaults."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisConfig:
    algorithm_version: str = "enhanced-analysis-v1"
    config_version: str = "enhanced-analysis-config-v2"
    vibration_sample_rate_hz: int = 64_000
    min_context_packets: int = 16
    detrend: str = "constant"
    vibration_highpass_hz: float = 5.0
    filter_order: int = 4
    psd_segment_seconds: float = 0.25
    psd_overlap_ratio: float = 0.5
    peak_min_snr_db: float = 6.0
    peak_min_distance_hz: float = 2.0
    default_resonance_band_hz: tuple[float, float] = (5_000.0, 15_000.0)
    stft_enabled: bool = False
    frame_seconds: float = 0.064
    frame_overlap_ratio: float = 0.75
    harmonic_max_order: int = 5
    tolerance_resolution_multiplier: float = 1.5
    tolerance_ratio: float = 0.02
    min_match_score: float = 0.60
    history_lookback_days: int = 30
    min_baseline_samples: int = 30
    similar_case_limit: int = 5
    clipping_ratio_warn: float = 0.005
    clipping_ratio_fail: float = 0.05
    max_nan_ratio: float = 0.0


DEFAULT_ANALYSIS_CONFIG = AnalysisConfig()
