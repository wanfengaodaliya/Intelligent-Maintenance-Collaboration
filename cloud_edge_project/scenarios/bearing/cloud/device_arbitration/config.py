from __future__ import annotations

from dataclasses import dataclass

from core.bearing_actions import ACTION_TO_GRADE, ACTION_TO_STATE


@dataclass(frozen=True)
class BearingArbitrationConfig:
    rule_version: str = "bearing-arbitration-v1"
    abnormal_min_confidence: float = 0.90
    multiple_high_risk_min_count: int = 2
    multiple_high_risk_min_confidence: float = 0.85
    min_top_score: float = 0.40
    min_margin: float = 0.05


DEFAULT_CONFIG = BearingArbitrationConfig()

ACTION_SEVERITY = ACTION_TO_GRADE
