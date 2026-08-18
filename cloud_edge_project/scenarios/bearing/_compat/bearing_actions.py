"""Shared bearing action levels used by edge aggregation and cloud arbitration.

This is the real implementation. core/bearing_actions.py is a compatibility shim.
"""

from __future__ import annotations


ACTION_TO_GRADE = {
    "continue_operation": 0,
    "enhanced_monitoring": 1,
    "scheduled_inspection": 2,
    "urgent_intervention": 3,
    "shutdown": 4,
}

GRADE_TO_ACTION = {grade: action for action, grade in ACTION_TO_GRADE.items()}

ACTION_TO_STATE = {
    "continue_operation": "normal",
    "enhanced_monitoring": "normal",
    "scheduled_inspection": "warning",
    "urgent_intervention": "warning",
    "shutdown": "abnormal",
}


def action_for_grade(grade: int) -> str:
    try:
        return GRADE_TO_ACTION[grade]
    except KeyError as exc:
        raise ValueError("action grade must be an integer from 0 to 4") from exc


def grade_for_action(action: str) -> int:
    try:
        return ACTION_TO_GRADE[action]
    except KeyError as exc:
        raise ValueError("unsupported bearing action: %s" % action) from exc