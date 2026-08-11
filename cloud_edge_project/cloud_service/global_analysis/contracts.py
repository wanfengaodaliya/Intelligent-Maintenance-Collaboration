"""Stable contracts and configurable thresholds for global analysis."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GlobalAnalysisConfig:
    default_task_limit: int = 20
    min_device_task_count: int = 5
    min_packet_review_count: int = 10
    min_condition_sample_count: int = 5
    packet_correction_warning_rate: float = 0.15
    min_bearing_review_count: int = 5
    bearing_correction_warning_rate: float = 0.20
    trend_threshold: float = 0.30
    conflict_rate_target: float = 0.05
    arbitration_success_target: float = 0.90
    condition_thresholds: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "shaft_speed_rpm": (800.0, 1600.0),
            "load_torque_nm": (100.0, 300.0),
            "bearing_radial_load_n": (500.0, 1000.0),
            "bearing_module_temperature_c": (60.0, 80.0),
        }
    )


DEFAULT_CONFIG = GlobalAnalysisConfig()
DEFAULT_TASK_LIMIT = DEFAULT_CONFIG.default_task_limit
