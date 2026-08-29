from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.action_level_contract import (
    ACTION_SCORER_VERSION,
    CONFLICT_SEMANTICS,
    FINAL_DECISION_SEMANTICS,
)


SUMMARY_WINDOW_SYNC_FIELDS = (
    "summary_result_id",
    "summary_window_id",
    "device_id",
    "run_id",
    "window_start_sequence",
    "window_end_sequence",
    "result_status",
    "revision",
    "has_conflict",
    "conflict_semantics",
    "action_scorer_version",
    "final_decision_semantics",
    "state_mismatch",
    "state_mismatch_pair_count",
    "node_states",
    "final_state",
    "arbitration_status",
    "excluded_from_formal_metrics",
    "max_action_level_gap",
    "max_action_score_gap",
    "max_observed_action_level",
    "max_observed_action_score",
    "action_levels_by_edge",
    "action_scores_by_edge",
    "final_action_level",
    "recommended_action",
    "conflicting_pair_count",
    "closed_at_ns",
)


def build_summary_window_sync_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a local window onto the explicit Summary-to-Cloud contract."""

    projected = {
        "summary_result_id": str(payload["summary_result_id"]),
        "summary_window_id": str(payload["summary_window_id"]),
        "device_id": str(payload["device_id"]),
        "run_id": payload.get("run_id"),
        "window_start_sequence": int(payload["window_start_sequence"]),
        "window_end_sequence": int(payload["window_end_sequence"]),
        "result_status": str(payload["result_status"]),
        "revision": int(payload["revision"]),
        "has_conflict": bool(payload["has_conflict"]),
        "conflict_semantics": str(
            payload.get("conflict_semantics", CONFLICT_SEMANTICS)
        ),
        "action_scorer_version": str(
            payload.get("action_scorer_version", ACTION_SCORER_VERSION)
        ),
        "final_decision_semantics": str(
            payload.get("final_decision_semantics", FINAL_DECISION_SEMANTICS)
        ),
        "state_mismatch": bool(payload.get("state_mismatch", False)),
        "state_mismatch_pair_count": int(
            payload.get("state_mismatch_pair_count", 0)
        ),
        "node_states": dict(payload.get("node_states", {})),
        "final_state": payload.get("final_state"),
        "arbitration_status": payload.get("arbitration_status"),
        "excluded_from_formal_metrics": bool(
            payload["excluded_from_formal_metrics"]
        ),
        "max_action_level_gap": int(payload.get("max_action_level_gap", 0)),
        "max_action_score_gap": float(payload.get("max_action_score_gap", 0.0)),
        "max_observed_action_level": payload.get("max_observed_action_level"),
        "max_observed_action_score": payload.get("max_observed_action_score"),
        "action_levels_by_edge": dict(payload.get("action_levels_by_edge", {})),
        "action_scores_by_edge": dict(payload.get("action_scores_by_edge", {})),
        "final_action_level": payload.get("final_action_level"),
        "recommended_action": payload.get("recommended_action"),
        "conflicting_pair_count": int(payload.get("conflict_pair_count", 0)),
        "closed_at_ns": int(payload["closed_at_ns"]),
    }
    if tuple(projected) != SUMMARY_WINDOW_SYNC_FIELDS:
        raise AssertionError("summary sync projection fields drifted")
    return projected
