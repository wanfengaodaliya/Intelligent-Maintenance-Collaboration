from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from itertools import combinations
from typing import Any

from core.action_level_contract import (
    ACTION_LEVEL_TO_ACTION,
    ACTION_SCORER_VERSION,
    CONFLICT_LEVEL_GAP,
    CONFLICT_SEMANTICS,
    SCORE_GAP_ABS_TOLERANCE,
    action_level_for_score,
)
from core.arbitration_contracts import ArbitrationValidationError
from core.diagnosis_identity import build_summary_window_id


EXPECTED_BEARING_IDS = {"bearing_01", "bearing_02"}
EXPECTED_EDGE_NODE_IDS = {"edge_01", "edge_02"}
BINARY_BEARING_STATES = {"normal", "fault"}


def adapt_summary_arbitration_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "conflict_id",
        "summary_result_id",
        "summary_window_id",
        "device_id",
        "run_id",
        "window_start_sequence",
        "window_end_sequence",
        "comparison",
        "bearing_results",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        _invalid(f"missing device arbitration fields: {missing}")

    conflict_id = _text(payload["conflict_id"], "conflict_id")
    summary_result_id = _text(payload["summary_result_id"], "summary_result_id")
    summary_window_id = _text(payload["summary_window_id"], "summary_window_id")
    device_id = _text(payload["device_id"], "device_id")
    run_id = _text(payload["run_id"], "run_id")
    window_start = _positive_int(
        payload["window_start_sequence"], "window_start_sequence"
    )
    window_end = _positive_int(
        payload["window_end_sequence"], "window_end_sequence"
    )
    if window_end < window_start:
        _invalid("window_end_sequence must not precede window_start_sequence")
    expected_window_id = build_summary_window_id(
        device_id=device_id,
        run_id=run_id,
        window_start_sequence=window_start,
        window_end_sequence=window_end,
    )
    if summary_window_id != expected_window_id:
        _invalid("summary_window_id does not match its run and sequence identity")

    results = payload["bearing_results"]
    if not isinstance(results, list) or len(results) != len(EXPECTED_BEARING_IDS):
        _invalid("bearing_results must contain exactly two results")

    scenario_results: list[dict[str, Any]] = []
    normalized_results: list[dict[str, Any]] = []
    result_ids: set[str] = set()
    bearing_ids: set[str] = set()
    edge_ids: set[str] = set()
    for value in results:
        if not isinstance(value, Mapping):
            _invalid("each bearing result must be an object")
        result_id = _text(value.get("result_id"), "result_id")
        bearing_id = _text(value.get("bearing_id"), "bearing_id")
        edge_node_id = _text(value.get("edge_node_id"), "edge_node_id")
        task_id = _text(value.get("task_id"), "task_id")
        sender_id = _text(value.get("sender_id"), "sender_id")
        if value.get("run_id") != run_id:
            _invalid("bearing result run_id does not match request run_id")
        if result_id in result_ids:
            _invalid("result_id values must be unique")
        if bearing_id in bearing_ids:
            _invalid("bearing_id values must be unique")
        result_ids.add(result_id)
        bearing_ids.add(bearing_id)
        edge_ids.add(edge_node_id)

        if value.get("device_id", device_id) != device_id:
            _invalid("bearing result device_id does not match request device_id")
        result_window_start = _positive_int(
            value.get("window_start_sequence", window_start),
            "bearing_result.window_start_sequence",
        )
        result_window_end = _positive_int(
            value.get("window_end_sequence", window_end),
            "bearing_result.window_end_sequence",
        )
        if result_window_start != window_start:
            _invalid("bearing result window_start_sequence does not match request")
        if result_window_end != window_end:
            _invalid("bearing result window_end_sequence does not match request")

        confidence = _score(value.get("confidence"), "confidence")
        data_quality_score = _score(
            value.get("data_quality_score"), "data_quality_score"
        )
        risk_level = _enum(
            value.get("risk_level"), "risk_level", {"low", "medium", "high"}
        )
        bearing_state = _enum(
            value.get("bearing_state"), "bearing_state", BINARY_BEARING_STATES
        )

        # action_scorer_v1 fields are authoritative for the arbitration input.
        scorer_version = _text(
            value.get("action_scorer_version"), "action_scorer_version"
        )
        if scorer_version != ACTION_SCORER_VERSION:
            _invalid("action_scorer_version is not supported")
        action_score = _score(value.get("action_score"), "action_score")
        action_level = _action_level(value.get("action_level"))
        if action_level_for_score(action_score) != action_level:
            raise ArbitrationValidationError(
                "CONFLICT_COMPARISON_MISMATCH",
                "action_level does not match action_score thresholds",
            )
        scored_action = _text(value.get("scored_action"), "scored_action")
        if scored_action != ACTION_LEVEL_TO_ACTION[action_level]:
            raise ArbitrationValidationError(
                "CONFLICT_COMPARISON_MISMATCH",
                "scored_action does not match action_level",
            )
        normalized_results.append(
            {
                "result_id": result_id,
                "bearing_id": bearing_id,
                "edge_node_id": edge_node_id,
                "bearing_state": bearing_state,
                "action_score": action_score,
                "action_level": action_level,
                "scored_action": scored_action,
            }
        )
        scenario_results.append(
            {
                "bearing_id": bearing_id,
                "bearing_state": bearing_state,
                "confidence": confidence,
                "data_quality_score": data_quality_score,
                "risk_level": risk_level,
                # DecisionUnit consumes the scorer-derived action.
                "recommended_action": scored_action,
                "scenario_payload": {
                    "result_id": result_id,
                    "task_id": task_id,
                    "sender_id": sender_id,
                    "edge_node_id": edge_node_id,
                    "action_score": action_score,
                    "action_level": action_level,
                    "action_scorer_version": scorer_version,
                },
            }
        )

    if bearing_ids != EXPECTED_BEARING_IDS:
        _invalid("bearing_results must contain bearing_01 and bearing_02")
    if edge_ids != EXPECTED_EDGE_NODE_IDS:
        _invalid("bearing_results must come from edge_01 and edge_02")

    # Cloud recomputes the action-level conflict and the state divergence
    # independently; neither derives from the other.
    node_states = {
        str(result["edge_node_id"]): str(result["bearing_state"])
        for result in normalized_results
    }
    action_levels_by_edge = {
        str(result["edge_node_id"]): int(result["action_level"])
        for result in normalized_results
    }
    action_scores_by_edge = {
        str(result["edge_node_id"]): float(result["action_score"])
        for result in normalized_results
    }
    state_mismatch = len(set(node_states.values())) > 1
    state_mismatch_pair_count = 1 if state_mismatch else 0
    conflicting_pair_count = sum(
        1
        for left, right in combinations(normalized_results, 2)
        if abs(int(left["action_level"]) - int(right["action_level"]))
        >= CONFLICT_LEVEL_GAP
    )
    max_action_level_gap = max(
        (
            abs(int(left["action_level"]) - int(right["action_level"]))
            for left, right in combinations(normalized_results, 2)
        ),
        default=0,
    )
    max_action_score_gap = max(
        (
            abs(float(left["action_score"]) - float(right["action_score"]))
            for left, right in combinations(normalized_results, 2)
        ),
        default=0.0,
    )
    if max_action_level_gap < CONFLICT_LEVEL_GAP:
        raise ArbitrationValidationError(
            "NOT_A_CONFLICT",
            "action level gap is below the conflict threshold",
        )

    comparison = payload["comparison"]
    if not isinstance(comparison, Mapping):
        _invalid("comparison must be an object")

    reported_type = _text(comparison.get("comparison_type"), "comparison_type")
    if reported_type != CONFLICT_SEMANTICS:
        _invalid("comparison_type is not supported")
    reported_scorer_version = _text(
        comparison.get("action_scorer_version"), "action_scorer_version"
    )
    if reported_scorer_version != ACTION_SCORER_VERSION:
        _invalid("comparison.action_scorer_version is not supported")
    reported_threshold = _non_negative_int(
        comparison.get("conflict_level_gap_threshold"),
        "comparison.conflict_level_gap_threshold",
    )
    if reported_threshold != CONFLICT_LEVEL_GAP:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported conflict_level_gap_threshold does not match",
        )

    reported_levels = _reported_int_mapping(
        comparison.get("action_levels_by_edge"), "comparison.action_levels_by_edge"
    )
    if reported_levels != action_levels_by_edge:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported action_levels_by_edge does not match Cloud recomputation",
        )
    reported_scores = _reported_score_mapping(
        comparison.get("action_scores_by_edge"), "comparison.action_scores_by_edge"
    )
    if set(reported_scores) != set(action_scores_by_edge) or any(
        abs(reported_scores[edge] - action_scores_by_edge[edge])
        > SCORE_GAP_ABS_TOLERANCE
        for edge in reported_scores
    ):
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported action_scores_by_edge does not match Cloud recomputation",
        )

    if "max_action_level_gap" in comparison:
        reported_level_gap = _non_negative_int(
            comparison.get("max_action_level_gap"),
            "comparison.max_action_level_gap",
        )
        if reported_level_gap != max_action_level_gap:
            raise ArbitrationValidationError(
                "CONFLICT_COMPARISON_MISMATCH",
                "reported max_action_level_gap does not match Cloud recomputation",
            )
    else:
        _invalid("comparison.max_action_level_gap is required")

    if "max_action_score_gap" in comparison:
        reported_score_gap = _score(
            comparison.get("max_action_score_gap"),
            "comparison.max_action_score_gap",
        )
        if abs(reported_score_gap - max_action_score_gap) > SCORE_GAP_ABS_TOLERANCE:
            raise ArbitrationValidationError(
                "CONFLICT_COMPARISON_MISMATCH",
                "reported max_action_score_gap does not match Cloud recomputation",
            )
    else:
        _invalid("comparison.max_action_score_gap is required")

    reported_conflict_count = _non_negative_int(
        comparison.get("conflicting_pair_count"),
        "comparison.conflicting_pair_count",
    )
    if reported_conflict_count != conflicting_pair_count:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported conflicting_pair_count does not match Cloud recomputation",
        )

    reported_node_states = comparison.get("node_states")
    if not isinstance(reported_node_states, Mapping):
        _invalid("comparison.node_states is required")
    reported_states = {
        str(edge).strip(): str(state)
        for edge, state in reported_node_states.items()
    }
    if reported_states != node_states:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported node_states does not match Cloud recomputation",
        )
    reported_mismatch = comparison.get("state_mismatch")
    if not isinstance(reported_mismatch, bool) or reported_mismatch != state_mismatch:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported state_mismatch does not match Cloud recomputation",
        )
    reported_state_mismatch_pairs = _non_negative_int(
        comparison.get("state_mismatch_pair_count"),
        "comparison.state_mismatch_pair_count",
    )
    if reported_state_mismatch_pairs != state_mismatch_pair_count:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported state_mismatch_pair_count does not match Cloud recomputation",
        )

    payload_hash = hashlib.sha256(
        json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return {
        "scenario_type": "bearing",
        "conflict_id": conflict_id,
        "subject_id": device_id,
        "task_id": summary_result_id,
        "scenario_payload": {"bearing_results": scenario_results},
        "summary_identity": {
            "summary_result_id": summary_result_id,
            "summary_window_id": summary_window_id,
            "device_id": device_id,
            "run_id": run_id,
            "window_start_sequence": window_start,
            "window_end_sequence": window_end,
            "bearing_result_ids": sorted(result_ids),
            "comparison": {
                "comparison_type": CONFLICT_SEMANTICS,
                "action_scorer_version": ACTION_SCORER_VERSION,
                "conflict_level_gap_threshold": CONFLICT_LEVEL_GAP,
                "action_levels_by_edge": action_levels_by_edge,
                "action_scores_by_edge": action_scores_by_edge,
                "max_action_level_gap": max_action_level_gap,
                "max_action_score_gap": max_action_score_gap,
                "conflicting_pair_count": conflicting_pair_count,
                "node_states": node_states,
                "state_mismatch": state_mismatch,
                "state_mismatch_pair_count": state_mismatch_pair_count,
            },
        },
        "request_payload_hash": payload_hash,
    }


def attach_summary_identity(
    result: Mapping[str, Any], adapted: Mapping[str, Any]
) -> dict[str, Any]:
    return {**result, **adapted["summary_identity"]}


def _invalid(message: str) -> None:
    raise ArbitrationValidationError("INVALID_REQUEST", message)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{field} is required")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    result = _non_negative_int(value, field)
    if result < 1:
        _invalid(f"{field} must be positive")
    return result


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid(f"{field} must be a non-negative integer")
    return value


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        _invalid(f"{field} must be in [0, 1]")
    return result


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    result = _text(value, field).lower()
    if result not in allowed:
        _invalid(f"{field} is not supported")
    return result


def _action_level(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(4):
        _invalid("action_level must be an integer from 0 to 3")
    return value


def _reported_int_mapping(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        _invalid(f"{field} must be an object")
    result: dict[str, int] = {}
    for edge, raw_level in value.items():
        edge_id = str(edge).strip()
        if not edge_id:
            _invalid(f"{field} keys must not be empty")
        result[edge_id] = _action_level(raw_level)
    return result


def _reported_score_mapping(value: Any, field: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        _invalid(f"{field} must be an object")
    result: dict[str, float] = {}
    for edge, raw_score in value.items():
        edge_id = str(edge).strip()
        if not edge_id:
            _invalid(f"{field} keys must not be empty")
        result[edge_id] = _score(raw_score, field)
    return result
