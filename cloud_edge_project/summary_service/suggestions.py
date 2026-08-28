"""Compatibility exports for bearing maintenance suggestion rules."""

from scenarios.bearing.summary_service.suggestions import (
    FALLBACK_BY_ACTION,
    GRADE_BY_ACTION,
    action_grade_for,
    build_final_suggestion,
)

__all__ = [
    "FALLBACK_BY_ACTION",
    "GRADE_BY_ACTION",
    "action_grade_for",
    "build_final_suggestion",
]
