from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BearingArbitrationConfig:
    rule_version: str = "bearing-arbitration-v1"
    abnormal_min_confidence: float = 0.90
    multiple_high_risk_min_count: int = 2
    multiple_high_risk_min_confidence: float = 0.85
    min_top_score: float = 0.40
    min_margin: float = 0.05


DEFAULT_CONFIG = BearingArbitrationConfig()

ACTION_TO_STATE = {
    "continue_operation": "normal",
    "enhanced_monitoring": "normal",
    "scheduled_inspection": "warning",
    "urgent_intervention": "warning",
    "shutdown": "abnormal",
}

ACTION_SEVERITY = {
    "continue_operation": 0,
    "enhanced_monitoring": 1,
    "scheduled_inspection": 2,
    "urgent_intervention": 3,
    "shutdown": 4,
}
