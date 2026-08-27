from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from itertools import combinations
from typing import Any

from core.arbitration_contracts import ArbitrationValidationError
from core.bearing_actions import ACTION_TO_STATE, action_for_grade


EXPECTED_BEARING_IDS = {"bearing_01", "bearing_02"}


def adapt_summary_arbitration_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "conflict_id",
        "summary_result_id",
        "device_id",
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
    device_id = _text(payload["device_id"], "device_id")
    window_start = _positive_int(
        payload["window_start_sequence"], "window_start_sequence"
    )
    window_end = _positive_int(
        payload["window_end_sequence"], "window_end_sequence"
    )
    if window_end < window_start:
        _invalid("window_end_sequence must not precede window_start_sequence")

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

        action_grade = _action_grade(value.get("action_grade"))
        recommended_action = _text(
            value.get("recommended_action"), "recommended_action"
        )
        if recommended_action != action_for_grade(action_grade):
            _invalid("recommended_action does not match action_grade")
        confidence = _score(value.get("confidence"), "confidence")
        data_quality_score = _score(
            value.get("data_quality_score"), "data_quality_score"
        )
        risk_level = _enum(
            value.get("risk_level"), "risk_level", {"low", "medium", "high"}
        )
        bearing_state = _enum(
            value.get("bearing_state", ACTION_TO_STATE[recommended_action]),
            "bearing_state",
            {"normal", "warning", "fault", "unknown"},
        )
        normalized_results.append(
            {
                "result_id": result_id,
                "bearing_id": bearing_id,
                "edge_node_id": edge_node_id,
                "action_grade": action_grade,
            }
        )
        scenario_results.append(
            {
                "bearing_id": bearing_id,
                "bearing_state": bearing_state,
                "confidence": confidence,
                "data_quality_score": data_quality_score,
                "risk_level": risk_level,
                "recommended_action": recommended_action,
                "scenario_payload": {
                    "result_id": result_id,
                    "task_id": task_id,
                    "sender_id": sender_id,
                    "edge_node_id": edge_node_id,
                    "action_grade": action_grade,
                },
            }
        )

    if bearing_ids != EXPECTED_BEARING_IDS:
        _invalid("bearing_results must contain bearing_01 and bearing_02")
    if len(edge_ids) < 2:
        _invalid("bearing_results must come from at least two edge nodes")

    comparisons = []
    for left, right in combinations(normalized_results, 2):
        if left["edge_node_id"] == right["edge_node_id"]:
            continue
        gap = abs(left["action_grade"] - right["action_grade"])
        comparisons.append(gap)
    max_gap = max(comparisons, default=0)
    conflict_count = sum(1 for gap in comparisons if gap >= 2)
    if max_gap < 2:
        raise ArbitrationValidationError(
            "NOT_A_CONFLICT", "maximum cross-edge action-grade gap is below 2"
        )

    comparison = payload["comparison"]
    if not isinstance(comparison, Mapping):
        _invalid("comparison must be an object")
    reported_gap = _non_negative_int(
        comparison.get("max_cross_edge_grade_gap"),
        "comparison.max_cross_edge_grade_gap",
    )
    reported_count = _non_negative_int(
        comparison.get("conflicting_pair_count"),
        "comparison.conflicting_pair_count",
    )
    if reported_gap != max_gap or reported_count != conflict_count:
        raise ArbitrationValidationError(
            "CONFLICT_COMPARISON_MISMATCH",
            "reported conflict comparison does not match Cloud recomputation",
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
            "device_id": device_id,
            "window_start_sequence": window_start,
            "window_end_sequence": window_end,
            "bearing_result_ids": sorted(result_ids),
            "comparison": {
                "max_cross_edge_grade_gap": max_gap,
                "conflicting_pair_count": conflict_count,
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


def _action_grade(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(5):
        _invalid("action_grade must be an integer from 0 to 4")
    return value
