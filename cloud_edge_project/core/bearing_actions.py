"""Compatibility shim for the historical core import path.

The compatibility package owns the dependency on the bearing plugin.
"""

from compatibility.bearing_v12.bearing_actions_exports import (
    ACTION_TO_GRADE,
    ACTION_TO_STATE,
    GRADE_TO_ACTION,
    action_for_grade,
    grade_for_action,
)

__all__ = [
    "ACTION_TO_GRADE",
    "ACTION_TO_STATE",
    "GRADE_TO_ACTION",
    "action_for_grade",
    "grade_for_action",
]
