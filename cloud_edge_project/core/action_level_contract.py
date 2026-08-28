"""Shared action-level contract used by Summary and Cloud.

Summary owns the full ``action_scorer_v1`` formula (entropy, uncertainty, risk
prior, probability validation).  Summary and Cloud share only this module's
thresholds, action mappings and conflict semantics so both sides validate the
same rules without duplicating the scorer formula.
"""

from __future__ import annotations

from typing import Any

ACTION_SCORER_VERSION = "action_scorer_v1"
CONFLICT_SEMANTICS = "action_level_gap_v1"
FINAL_DECISION_SEMANTICS = "action_derived_v1"

ACTION_SCORE_THRESHOLDS = (0.20, 0.45, 0.75)
CONFLICT_LEVEL_GAP = 3
SCORE_GAP_ABS_TOLERANCE = 1e-9

ACTION_LEVEL_TO_ACTION = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "shutdown",
}

# Final device disposition state derived from the maintenance action level.
# The final state is a pure function of the action level, NOT a bearing-state
# OR.  Raw per-bearing diagnostics stay in node_states / state_mismatch.
LEVEL_TO_STATE = {
    0: "normal",
    1: "normal",
    2: "warning",
    3: "fault",
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


def build_final_decision(action_level: int) -> dict[str, Any]:
    """Build the authoritative final-decision triple for a settled window.

    The final state and recommended action are both derived from the action
    level so that Summary-local and Cloud-arbitrated FINAL windows can never
    disagree on any of the three fields.
    """
    return {
        "final_state": LEVEL_TO_STATE[action_level],
        "final_action_level": int(action_level),
        "recommended_action": ACTION_LEVEL_TO_ACTION[action_level],
    }
