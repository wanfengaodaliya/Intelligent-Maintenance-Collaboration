from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from typing import Any

from core.action_level_contract import (
    ACTION_SCORER_VERSION,
    CONFLICT_LEVEL_GAP,
    CONFLICT_SEMANTICS,
    FINAL_DECISION_SEMANTICS,
    build_final_decision,
)

from .contracts import (
    EXPECTED_BEARING_IDS,
    EXPECTED_EDGE_NODE_IDS,
    group_key,
    stable_id,
)


def _window_identity(
    source_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    keys = {group_key(result) for result in source_results}
    if len(keys) != 1:
        raise ValueError("all bearing results must belong to the same device window")
    first = source_results[0]
    return {
        "summary_window_id": keys.pop(),
        "device_id": str(first["device_id"]),
        "run_id": first.get("run_id"),
        "window_start_sequence": int(first["window_start_sequence"]),
        "window_end_sequence": int(first["window_end_sequence"]),
    }


def _cross_edge_comparisons(
    source_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for left, right in combinations(source_results, 2):
        if left["edge_node_id"] == right["edge_node_id"]:
            continue
        left_level = int(left["action_level"])
        right_level = int(right["action_level"])
        comparisons.append(
            {
                "left_bearing_id": left["bearing_id"],
                "left_edge_node_id": left["edge_node_id"],
                "left_action_level": left_level,
                "left_action_score": float(left["action_score"]),
                "left_bearing_state": str(left["bearing_state"]),
                "right_bearing_id": right["bearing_id"],
                "right_edge_node_id": right["edge_node_id"],
                "right_action_level": right_level,
                "right_action_score": float(right["action_score"]),
                "right_bearing_state": str(right["bearing_state"]),
                "level_gap": abs(left_level - right_level),
                "score_gap": abs(
                    float(left["action_score"]) - float(right["action_score"])
                ),
                "state_mismatch": str(left["bearing_state"])
                != str(right["bearing_state"]),
                # Action-level conflict is the only conflict signal.
                "is_conflict": abs(left_level - right_level) >= CONFLICT_LEVEL_GAP,
            }
        )
    return comparisons


def _max_level_gap(comparisons: Sequence[Mapping[str, Any]]) -> int:
    return max(
        (int(comparison["level_gap"]) for comparison in comparisons), default=0
    )


def _max_score_gap(comparisons: Sequence[Mapping[str, Any]]) -> float:
    return max(
        (float(comparison["score_gap"]) for comparison in comparisons), default=0.0
    )


def build_window_result(
    results: Iterable[Mapping[str, Any]],
    *,
    closed_at_ns: int,
    expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
    expected_edge_node_ids: Sequence[str] = EXPECTED_EDGE_NODE_IDS,
) -> dict[str, Any]:
    source_results = sorted(
        (dict(result) for result in results), key=lambda item: str(item["bearing_id"])
    )
    expected = tuple(expected_bearing_ids)
    actual = tuple(result["bearing_id"] for result in source_results)
    if actual != tuple(sorted(expected)):
        raise ValueError(f"expected one result for each bearing {expected}, got {actual}")

    identity = _window_identity(source_results)
    device_id = identity["device_id"]
    window_start = identity["window_start_sequence"]
    window_end = identity["window_end_sequence"]
    summary_window_id = identity["summary_window_id"]

    edge_ids = sorted({str(result["edge_node_id"]) for result in source_results})
    sufficient_edge_diversity = set(edge_ids) == set(expected_edge_node_ids)

    comparisons = _cross_edge_comparisons(source_results)
    node_states = {
        str(result["edge_node_id"]): str(result["bearing_state"])
        for result in source_results
    }

    # State divergence is an observational field, fully independent of the
    # action-level conflict signal.
    state_mismatch = sufficient_edge_diversity and len(set(node_states.values())) > 1
    state_mismatch_pair_count = sum(
        1 for comparison in comparisons if comparison["state_mismatch"]
    )
    conflict_pair_count = sum(
        1 for comparison in comparisons if comparison["is_conflict"]
    )
    has_conflict = sufficient_edge_diversity and conflict_pair_count > 0

    max_action_level_gap = _max_level_gap(comparisons)
    max_action_score_gap = _max_score_gap(comparisons)
    max_observed_action_level = max(
        (int(result["action_level"]) for result in source_results), default=0
    )
    max_observed_action_score = max(
        (float(result["action_score"]) for result in source_results), default=0.0
    )
    action_levels_by_edge: dict[str, int] = {
        str(result["edge_node_id"]): int(result["action_level"])
        for result in source_results
    }
    action_scores_by_edge: dict[str, float] = {
        str(result["edge_node_id"]): float(result["action_score"])
        for result in source_results
    }

    # Final decision is only produced for a settled, non-conflicted window.
    # The triple is generated by one shared rule so the state can never
    # contradict the action.  Raw bearing diagnostics stay observational.
    if sufficient_edge_diversity and not has_conflict:
        decision = build_final_decision(max_observed_action_level)
        final_action_level = decision["final_action_level"]
        recommended_action = decision["recommended_action"]
        final_state = decision["final_state"]
    else:
        final_action_level = None
        recommended_action = None
        final_state = None

    summary_result_id = stable_id("summary", summary_window_id)
    conflict_id = (
        stable_id("conflict", summary_window_id) if has_conflict else None
    )

    return {
        "summary_result_id": summary_result_id,
        "summary_window_id": summary_window_id,
        "conflict_id": conflict_id,
        "device_id": device_id,
        "run_id": identity["run_id"],
        "window_start_sequence": window_start,
        "window_end_sequence": window_end,
        "result_status": (
            "INCOMPLETE"
            if not sufficient_edge_diversity
            else "PENDING_ARBITRATION"
            if has_conflict
            else "FINAL"
        ),
        "arbitration_status": (
            "PENDING" if has_conflict and sufficient_edge_diversity else None
        ),
        "excluded_from_formal_metrics": not sufficient_edge_diversity,
        "incomplete_reason": None if sufficient_edge_diversity else "INSUFFICIENT_EDGE_DIVERSITY",
        "has_conflict": has_conflict,
        "conflict_semantics": CONFLICT_SEMANTICS,
        "action_scorer_version": ACTION_SCORER_VERSION,
        "final_decision_semantics": FINAL_DECISION_SEMANTICS,
        "node_states": node_states,
        "state_mismatch": state_mismatch,
        "state_mismatch_pair_count": state_mismatch_pair_count,
        "final_state": final_state,
        "cross_edge_pair_count": len(comparisons),
        "conflict_pair_count": conflict_pair_count,
        "max_action_level_gap": max_action_level_gap,
        "max_action_score_gap": max_action_score_gap,
        "max_observed_action_level": max_observed_action_level,
        "max_observed_action_score": max_observed_action_score,
        "edge_node_ids": edge_ids,
        "action_levels_by_edge": action_levels_by_edge,
        "action_scores_by_edge": action_scores_by_edge,
        "comparisons": comparisons,
        "final_action_level": final_action_level,
        "recommended_action": recommended_action,
        "confidence": min(float(result["confidence"]) for result in source_results),
        "data_quality_score": min(
            float(result["data_quality_score"]) for result in source_results
        ),
        "source_results": source_results,
        "closed_at_ns": int(closed_at_ns),
    }


def build_arbitration_request(window_result: Mapping[str, Any]) -> dict[str, Any]:
    if not window_result.get("has_conflict"):
        raise ValueError("only conflicted windows may be submitted for arbitration")
    return {
        "conflict_id": window_result["conflict_id"],
        "summary_result_id": window_result["summary_result_id"],
        "summary_window_id": window_result["summary_window_id"],
        "device_id": window_result["device_id"],
        "run_id": window_result.get("run_id"),
        "window_start_sequence": int(window_result["window_start_sequence"]),
        "window_end_sequence": int(window_result["window_end_sequence"]),
        "comparison": {
            "comparison_type": CONFLICT_SEMANTICS,
            "action_scorer_version": ACTION_SCORER_VERSION,
            "conflict_level_gap_threshold": CONFLICT_LEVEL_GAP,
            "action_levels_by_edge": dict(window_result["action_levels_by_edge"]),
            "action_scores_by_edge": dict(window_result["action_scores_by_edge"]),
            "max_action_level_gap": int(window_result["max_action_level_gap"]),
            "max_action_score_gap": float(window_result["max_action_score_gap"]),
            "conflicting_pair_count": int(window_result["conflict_pair_count"]),
            "node_states": dict(window_result["node_states"]),
            "state_mismatch": bool(window_result["state_mismatch"]),
            "state_mismatch_pair_count": int(
                window_result["state_mismatch_pair_count"]
            ),
        },
        "bearing_results": list(window_result["source_results"]),
    }


def build_incomplete_window_result(
    results: Iterable[Mapping[str, Any]],
    *,
    closed_at_ns: int,
    reason: str = "WINDOW_TIMEOUT",
    expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
) -> dict[str, Any]:
    source_results = sorted(
        (dict(result) for result in results), key=lambda item: str(item["bearing_id"])
    )
    if not source_results:
        raise ValueError("an incomplete window must contain at least one result")
    identity = _window_identity(source_results)
    device_id = identity["device_id"]
    window_start = identity["window_start_sequence"]
    window_end = identity["window_end_sequence"]

    actual = {str(result["bearing_id"]) for result in source_results}
    missing = sorted(set(expected_bearing_ids) - actual)
    if not missing:
        raise ValueError("use build_window_result when all expected bearings are present")
    edge_ids = sorted({str(result["edge_node_id"]) for result in source_results})
    comparisons = _cross_edge_comparisons(source_results)
    node_states = {
        str(result["edge_node_id"]): str(result["bearing_state"])
        for result in source_results
    }
    action_levels_by_edge: dict[str, int] = {
        str(result["edge_node_id"]): int(result["action_level"])
        for result in source_results
    }
    action_scores_by_edge: dict[str, float] = {
        str(result["edge_node_id"]): float(result["action_score"])
        for result in source_results
    }
    return {
        "summary_result_id": stable_id("summary", identity["summary_window_id"]),
        "summary_window_id": identity["summary_window_id"],
        "conflict_id": None,
        "device_id": device_id,
        "run_id": identity["run_id"],
        "window_start_sequence": window_start,
        "window_end_sequence": window_end,
        "result_status": "INCOMPLETE",
        "arbitration_status": None,
        "excluded_from_formal_metrics": True,
        "incomplete_reason": reason,
        "missing_bearing_ids": missing,
        "has_conflict": False,
        "conflict_semantics": CONFLICT_SEMANTICS,
        "action_scorer_version": ACTION_SCORER_VERSION,
        "final_decision_semantics": FINAL_DECISION_SEMANTICS,
        "node_states": node_states,
        "state_mismatch": False,
        "state_mismatch_pair_count": 0,
        "final_state": None,
        "cross_edge_pair_count": len(comparisons),
        "conflict_pair_count": 0,
        "max_action_level_gap": 0,
        "max_action_score_gap": 0.0,
        "max_observed_action_level": max(
            (int(result["action_level"]) for result in source_results), default=0
        ),
        "max_observed_action_score": max(
            (float(result["action_score"]) for result in source_results), default=0.0
        ),
        "edge_node_ids": edge_ids,
        "action_levels_by_edge": action_levels_by_edge,
        "action_scores_by_edge": action_scores_by_edge,
        "comparisons": comparisons,
        "final_action_level": None,
        "recommended_action": None,
        "confidence": min(float(result["confidence"]) for result in source_results),
        "data_quality_score": min(
            float(result["data_quality_score"]) for result in source_results
        ),
        "source_results": source_results,
        "closed_at_ns": int(closed_at_ns),
    }
