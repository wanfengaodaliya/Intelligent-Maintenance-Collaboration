"""Scenario-neutral runtime contracts for global analysis."""

from __future__ import annotations

from typing import Mapping, Protocol


DEFAULT_TASK_LIMIT = 20


class GlobalAnalysisRuntimeConfig(Protocol):
    min_device_task_count: int
    min_packet_review_count: int
    min_condition_sample_count: int
    packet_correction_warning_rate: float
    trend_threshold: float
    conflict_rate_target: float
    arbitration_success_target: float
    condition_thresholds: Mapping[str, tuple[float, float]]


__all__ = ["DEFAULT_TASK_LIMIT", "GlobalAnalysisRuntimeConfig"]
