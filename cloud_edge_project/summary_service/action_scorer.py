"""Compatibility exports for bearing summary action scoring."""

from scenarios.bearing.summary_service.action_scorer import (
    H5_CLASS_LABELS,
    R_UNCERTAIN_BY_RISK,
    SCORE_SUM_TOLERANCE,
    score_bearing_action,
)

__all__ = [
    "H5_CLASS_LABELS",
    "R_UNCERTAIN_BY_RISK",
    "SCORE_SUM_TOLERANCE",
    "score_bearing_action",
]
