"""Shared action-level contract used by Summary and Cloud.

Summary owns the full ``action_scorer_v1`` formula (entropy, uncertainty, risk
prior, probability validation).  Summary and Cloud share only this module's
thresholds, action mappings and conflict semantics so both sides validate the
same rules without duplicating the scorer formula.
"""

from __future__ import annotations

ACTION_SCORER_VERSION = "action_scorer_v1"
CONFLICT_SEMANTICS = "action_level_gap_v1"

ACTION_SCORE_THRESHOLDS = (0.20, 0.45, 0.75)
CONFLICT_LEVEL_GAP = 3
SCORE_GAP_ABS_TOLERANCE = 1e-9

ACTION_LEVEL_TO_ACTION = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "shutdown",
}

ACTION_LEVEL_TO_LEGACY_GRADE = {
    0: 0,
    1: 1,
    2: 2,
    3: 4,
}

# Reverse mapping used only for the Cloud arbitration write-back
# (final_action -> action_level); derived to avoid duplicated maintenance.
ACTION_TO_LEVEL = {action: level for level, action in ACTION_LEVEL_TO_ACTION.items()}


def action_level_for_score(score: float) -> int:
    """Map an action score to its 0-3 action level using shared thresholds."""
    if score < ACTION_SCORE_THRESHOLDS[0]:
        return 0
    if score < ACTION_SCORE_THRESHOLDS[1]:
        return 1
    if score < ACTION_SCORE_THRESHOLDS[2]:
        return 2
    return 3
