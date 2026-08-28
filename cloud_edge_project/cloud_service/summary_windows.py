from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from itertools import combinations
from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError
from cloud_service.storage.database import connect, initialize_database
from core.action_level_contract import (
    ACTION_LEVEL_TO_ACTION,
    ACTION_LEVEL_TO_LEGACY_GRADE,
    ACTION_SCORER_VERSION,
    CONFLICT_LEVEL_GAP,
    CONFLICT_SEMANTICS,
    SCORE_GAP_ABS_TOLERANCE,
)
from core.diagnosis_identity import build_summary_window_id


BINARY_BEARING_STATES = {"normal", "fault"}
ARBITRATION_STATUSES = {"PENDING", "RESOLVED", "MANUAL_REVIEW"}
WINDOW_STATUSES = {"FINAL", "PENDING_ARBITRATION", "INCOMPLETE", "MANUAL_REVIEW"}
CONFLICT_SEMANTICS_VALUES = {
    "binary_state",
    "legacy_grade_gap",
    "action_level_gap_v1",
}


class SummaryWindowRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def accept(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_summary_window(payload)
        payload_json = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        node_states_json = json.dumps(
            normalized["node_states"], ensure_ascii=False, sort_keys=True
        )
        row_values = (
            normalized["summary_result_id"],
            normalized["summary_window_id"],
            normalized["device_id"],
            normalized["run_id"],
            normalized["window_start_sequence"],
            normalized["window_end_sequence"],
            normalized["result_status"],
            normalized["revision"],
            int(normalized["has_conflict"]),
            int(normalized["state_mismatch"]),
            node_states_json,
            normalized["final_state"],
            normalized["arbitration_status"],
            normalized["conflict_semantics"],
            int(normalized["excluded_from_formal_metrics"]),
            normalized["max_cross_edge_grade_gap"],
            normalized["conflicting_pair_count"],
            payload_hash,
            payload_json,
            normalized["closed_at_ns"],
        )
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO summary_window_record (
                    summary_result_id, summary_window_id, device_id, run_id,
                    window_start_sequence,
                    window_end_sequence, result_status, revision, has_conflict,
                    state_mismatch, node_states_json, final_state,
                    arbitration_status, conflict_semantics, excluded_from_formal_metrics,
                    max_cross_edge_grade_gap, conflicting_pair_count,
                    payload_hash, payload_json, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                row_values,
            )
            row = connection.execute(
                """
                SELECT summary_result_id, summary_window_id, revision, payload_hash,
                       payload_json
                FROM summary_window_record
                WHERE summary_result_id = ? OR summary_window_id = ?
                """,
                (
                    normalized["summary_result_id"],
                    normalized["summary_window_id"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("summary window was not persisted")
            if (
                row["summary_result_id"] != normalized["summary_result_id"]
                or row["summary_window_id"] != normalized["summary_window_id"]
            ):
                raise ArbitrationPayloadConflictError(
                    "summary identity already belongs to a different window payload"
                )
            if row["payload_hash"] != payload_hash:
                # A window may be re-published after arbitration or late
                # completion; only a strictly newer revision may replace it.
                if normalized["revision"] <= int(row["revision"] or 0):
                    raise ArbitrationPayloadConflictError(
                        "summary_result_id already belongs to a different window payload"
                    )
                connection.execute(
                    """
                    UPDATE summary_window_record
                    SET result_status = ?, revision = ?, has_conflict = ?,
                        state_mismatch = ?, node_states_json = ?,
                        final_state = ?, arbitration_status = ?,
                        conflict_semantics = ?, excluded_from_formal_metrics = ?,
                        max_cross_edge_grade_gap = ?, conflicting_pair_count = ?,
                        payload_hash = ?,
                        payload_json = ?, created_at_ns = ?
                    WHERE summary_result_id = ?
                    """,
                    (
                        *row_values[6:],
                        normalized["summary_result_id"],
                    ),
                )
                return normalized
        return json.loads(row["payload_json"])

    def list_recent(
        self, *, device_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            if device_id:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM summary_window_record
                    WHERE device_id = ? ORDER BY created_at_ns DESC LIMIT ?
                    """,
                    (device_id, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM summary_window_record
                    ORDER BY created_at_ns DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]


def normalize_summary_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "summary_result_id",
        "summary_window_id",
        "device_id",
        "run_id",
        "window_start_sequence",
        "window_end_sequence",
        "result_status",
        "revision",
        "has_conflict",
        "state_mismatch",
        "node_states",
        "final_state",
        "arbitration_status",
        "excluded_from_formal_metrics",
        "max_cross_edge_grade_gap",
        "conflicting_pair_count",
        "closed_at_ns",
        "conflict_semantics",
        "action_scorer_version",
        "action_levels_by_edge",
        "action_scores_by_edge",
        "max_action_level_gap",
        "max_action_score_gap",
        "state_mismatch_pair_count",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing summary-window fields: {missing}")
    result = {field: payload[field] for field in required}
    for field in ("summary_result_id", "summary_window_id", "device_id", "run_id"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(f"{field} is required")
        result[field] = result[field].strip()
    if result["result_status"] not in WINDOW_STATUSES:
        raise ValueError("result_status is not supported")
    if result["conflict_semantics"] not in CONFLICT_SEMANTICS_VALUES:
        raise ValueError("conflict_semantics is not supported")
    if result["action_scorer_version"] != ACTION_SCORER_VERSION:
        raise ValueError("action_scorer_version is not supported")
    for field in ("has_conflict", "state_mismatch", "excluded_from_formal_metrics"):
        if not isinstance(result[field], bool):
            raise ValueError(f"{field} must be boolean")
    for field in (
        "window_start_sequence",
        "window_end_sequence",
        "revision",
        "max_cross_edge_grade_gap",
        "conflicting_pair_count",
        "max_action_level_gap",
        "state_mismatch_pair_count",
        "closed_at_ns",
    ):
        if isinstance(result[field], bool) or not isinstance(result[field], int):
            raise ValueError(f"{field} must be an integer")
        if result[field] < 0:
            raise ValueError(f"{field} must be non-negative")
    if result["window_start_sequence"] < 1:
        raise ValueError("window_start_sequence must be positive")
    if result["window_end_sequence"] < result["window_start_sequence"]:
        raise ValueError("window_end_sequence must not precede window_start_sequence")
    expected_window_id = build_summary_window_id(
        device_id=result["device_id"],
        run_id=result["run_id"],
        window_start_sequence=result["window_start_sequence"],
        window_end_sequence=result["window_end_sequence"],
    )
    if result["summary_window_id"] != expected_window_id:
        raise ValueError("summary_window_id does not match its run and sequence identity")
    if result["revision"] < 1:
        raise ValueError("revision must be positive")

    if isinstance(result["max_action_score_gap"], bool) or not isinstance(
        result["max_action_score_gap"], (int, float)
    ):
        raise ValueError("max_action_score_gap must be numeric")
    max_action_score_gap = float(result["max_action_score_gap"])
    if not 0.0 <= max_action_score_gap <= 1.0:
        raise ValueError("max_action_score_gap must be in [0, 1]")
    result["max_action_score_gap"] = max_action_score_gap

    node_states = _normalize_node_states(result["node_states"])
    result["node_states"] = node_states
    action_levels_by_edge = _normalize_level_mapping(result["action_levels_by_edge"])
    action_scores_by_edge = _normalize_score_mapping(result["action_scores_by_edge"])
    result["action_levels_by_edge"] = action_levels_by_edge
    result["action_scores_by_edge"] = action_scores_by_edge

    edge_keys = set(node_states)
    if set(action_levels_by_edge) != edge_keys or set(action_scores_by_edge) != edge_keys:
        raise ValueError(
            "action_levels_by_edge, action_scores_by_edge and node_states must share edge keys"
        )

    # Cross-check the reported gaps against the per-edge values.
    if _max_abs_gap_int(action_levels_by_edge) != result["max_action_level_gap"]:
        raise ValueError("max_action_level_gap does not match action_levels_by_edge")
    if abs(_max_abs_gap_float(action_scores_by_edge) - max_action_score_gap) > (
        SCORE_GAP_ABS_TOLERANCE
    ):
        raise ValueError("max_action_score_gap does not match action_scores_by_edge")

    if result["excluded_from_formal_metrics"]:
        if result["has_conflict"]:
            raise ValueError("an incomplete window cannot be counted as a conflict")
        if result["max_action_level_gap"] >= CONFLICT_LEVEL_GAP:
            raise ValueError("an incomplete window cannot carry an action-level conflict")
    else:
        if result["has_conflict"] != (
            result["max_action_level_gap"] >= CONFLICT_LEVEL_GAP
        ):
            raise ValueError(
                "has_conflict must equal (max_action_level_gap >= conflict threshold)"
            )
        if result["has_conflict"] and result["conflicting_pair_count"] < 1:
            raise ValueError("a conflicted window must report at least one conflicting pair")
        if not result["has_conflict"] and result["conflicting_pair_count"] != 0:
            raise ValueError("conflicting_pair_count must be zero without a conflict")

    if result["state_mismatch"] and result["state_mismatch_pair_count"] < 1:
        raise ValueError("state_mismatch_pair_count must be positive when state_mismatch is true")
    if not result["state_mismatch"] and result["state_mismatch_pair_count"] != 0:
        raise ValueError("state_mismatch_pair_count must be zero without a state mismatch")

    final_state = result["final_state"]
    if final_state is not None:
        if not isinstance(final_state, str) or final_state not in BINARY_BEARING_STATES:
            raise ValueError("final_state must be normal or fault")

    arbitration_status = result["arbitration_status"]
    if arbitration_status is not None:
        if not isinstance(arbitration_status, str) or (
            arbitration_status not in ARBITRATION_STATUSES
        ):
            raise ValueError("arbitration_status is not supported")

    final_action_level = _optional_action_level(payload.get("final_action_level"))
    final_action_grade = _optional_int(payload.get("final_action_grade"), 0, 4)
    recommended_action = payload.get("recommended_action")
    if recommended_action is not None and (
        not isinstance(recommended_action, str) or not recommended_action.strip()
    ):
        raise ValueError("recommended_action must be a non-empty string when present")
    result["final_action_level"] = final_action_level
    result["final_action_grade"] = final_action_grade
    result["recommended_action"] = recommended_action

    if final_action_level is not None:
        if final_action_grade != ACTION_LEVEL_TO_LEGACY_GRADE[final_action_level]:
            raise ValueError("final_action_grade does not match final_action_level")
        if recommended_action != ACTION_LEVEL_TO_ACTION[final_action_level]:
            raise ValueError("recommended_action does not match final_action_level")

    _validate_window_state(result)

    return result


def _validate_window_state(result: dict[str, Any]) -> None:
    status = result["result_status"]
    has_conflict = result["has_conflict"]
    arbitration_status = result["arbitration_status"]
    final_complete = (
        result["final_action_level"] is not None
        and result["final_action_grade"] is not None
        and result["recommended_action"] is not None
    )
    final_empty = (
        result["final_action_level"] is None
        and result["final_action_grade"] is None
        and result["recommended_action"] is None
    )
    if not (final_complete or final_empty):
        raise ValueError(
            "final_action_level, final_action_grade and recommended_action must be all set or all empty"
        )

    if status == "INCOMPLETE":
        if not result["excluded_from_formal_metrics"]:
            raise ValueError("an INCOMPLETE window must be excluded from formal metrics")
        if has_conflict:
            raise ValueError("an INCOMPLETE window cannot be a conflict")
        if not final_empty:
            raise ValueError("an INCOMPLETE window must not carry a final action")
        if arbitration_status is not None:
            raise ValueError("an INCOMPLETE window must not carry arbitration")
    elif status == "PENDING_ARBITRATION":
        if not has_conflict:
            raise ValueError("a PENDING_ARBITRATION window must be a conflict")
        if not final_empty:
            raise ValueError("a PENDING_ARBITRATION window must not carry a final action")
        if arbitration_status != "PENDING":
            raise ValueError("a PENDING_ARBITRATION window must have arbitration_status PENDING")
    elif status == "MANUAL_REVIEW":
        if not has_conflict:
            raise ValueError("a MANUAL_REVIEW window must be a conflict")
        if not final_empty:
            raise ValueError("a MANUAL_REVIEW window must not carry a final action")
        if arbitration_status != "MANUAL_REVIEW":
            raise ValueError("a MANUAL_REVIEW window must have arbitration_status MANUAL_REVIEW")
    elif status == "FINAL":
        if not final_complete:
            raise ValueError("a FINAL window must carry a complete final action")
        if has_conflict:
            if arbitration_status != "RESOLVED":
                raise ValueError("an arbitrated FINAL window must be RESOLVED")
        else:
            if arbitration_status is not None:
                raise ValueError("a non-arbitrated FINAL window must not carry arbitration")


def _normalize_node_states(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("node_states must be a non-empty object")
    states: dict[str, str] = {}
    for edge, state in value.items():
        edge_id = str(edge).strip()
        if not edge_id:
            raise ValueError("node_states keys must not be empty")
        if not isinstance(state, str) or state not in BINARY_BEARING_STATES:
            raise ValueError("node_states values must be normal or fault")
        states[edge_id] = state
    return states


def _normalize_level_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("action_levels_by_edge must be an object")
    levels: dict[str, int] = {}
    for edge, raw_level in value.items():
        edge_id = str(edge).strip()
        if not edge_id:
            raise ValueError("action_levels_by_edge keys must not be empty")
        if isinstance(raw_level, bool) or not isinstance(raw_level, int):
            raise ValueError("action_levels_by_edge values must be integers")
        if raw_level not in range(4):
            raise ValueError("action_levels_by_edge values must be between 0 and 3")
        levels[edge_id] = raw_level
    return levels


def _normalize_score_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("action_scores_by_edge must be an object")
    scores: dict[str, float] = {}
    for edge, raw_score in value.items():
        edge_id = str(edge).strip()
        if not edge_id:
            raise ValueError("action_scores_by_edge keys must not be empty")
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError("action_scores_by_edge values must be numeric")
        score = float(raw_score)
        if not 0.0 <= score <= 1.0:
            raise ValueError("action_scores_by_edge values must be in [0, 1]")
        scores[edge_id] = score
    return scores


def _optional_action_level(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(4):
        raise ValueError("final_action_level must be an integer from 0 to 3 or null")
    return value


def _optional_int(value: Any, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"value must be an integer from {low} to {high} or null")
    return value


def _max_abs_gap_int(values: Mapping[str, int]) -> int:
    items = list(values.values())
    return max(
        (abs(items[i] - items[j]) for i in range(len(items)) for j in range(i + 1, len(items))),
        default=0,
    )


def _max_abs_gap_float(values: Mapping[str, float]) -> float:
    items = list(values.values())
    return max(
        (abs(items[i] - items[j]) for i in range(len(items)) for j in range(i + 1, len(items))),
        default=0.0,
    )
