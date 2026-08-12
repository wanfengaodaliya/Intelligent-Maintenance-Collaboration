"""Stable contracts and configurable gates for cloud model updates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelUpdateConfig:
    """Configuration shared by update decisions, datasets and validation."""

    min_update_evidence_count: int = 20
    min_focus_sample_count: int = 1
    train_ratio: float = 0.70
    validation_ratio: float = 0.15
    test_ratio: float = 0.15
    min_target_improvement: float = 0.01
    max_overall_metric_degradation: float = 0.02
    post_improvement_threshold: float = 0.01
    post_regression_threshold: float = 0.02

    def __post_init__(self) -> None:
        if self.min_update_evidence_count < 1 or self.min_focus_sample_count < 1:
            raise ValueError("sample thresholds must be positive")
        ratios = (self.train_ratio, self.validation_ratio, self.test_ratio)
        if any(ratio <= 0 for ratio in ratios) or abs(sum(ratios) - 1.0) > 1e-9:
            raise ValueError("dataset split ratios must be positive and sum to one")


DEFAULT_CONFIG = ModelUpdateConfig()

DATA_PREPARATION_STATES = {"created", "data_preparing", "data_prepare_failed"}
TRAINING_RESULT_STATES = {"training"}
VALIDATION_STATES = {"trained", "validation_failed"}
CONFIRMATION_STATES = {"waiting_confirmation"}
DISTRIBUTION_HANDOFF_STATES = {"approved"}
