from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from core.diagnosis_identity import (
    build_summary_window_id as build_shared_summary_window_id,
)

from .action_scorer import H5_CLASS_LABELS, score_bearing_action


EXPECTED_BEARING_IDS = ("bearing_01", "bearing_02")
EXPECTED_EDGE_NODE_IDS = ("edge_01", "edge_02")
BINARY_BEARING_STATES = {"normal", "fault"}
RISK_LEVELS = {"low", "medium", "high"}

ACTION_BY_GRADE = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}

GRADE_BY_ACTION = {action: grade for grade, action in ACTION_BY_GRADE.items()}


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def build_summary_window_id(
    device_id: str,
    run_id: str | None,
    window_start_sequence: int,
    window_end_sequence: int,
) -> str:
    """Stable shared identity for one summary window across all senders."""

    return build_shared_summary_window_id(
        device_id=device_id,
        run_id=run_id,
        window_start_sequence=int(window_start_sequence),
        window_end_sequence=int(window_end_sequence),
    )


def _normalize_run_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("run_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("run_id must be a string when present")
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > 128:
        raise ValueError("run_id must not exceed 128 characters")
    return stripped


def _require_class_probabilities(
    payload: Mapping[str, Any]
) -> dict[str, Any]:
    value = payload.get("class_probabilities")
    if value is None:
        raise ValueError("class_probabilities are required by action_scorer_v1")
    if not isinstance(value, Mapping):
        raise ValueError("class_probabilities must be an object")
    probabilities: dict[str, Any] = {}
    for label, raw_score in value.items():
        label = str(label).strip()
        if not label:
            raise ValueError("class_probabilities labels must not be empty")
        probabilities[label] = raw_score
    if set(probabilities) != set(H5_CLASS_LABELS):
        raise ValueError(
            "class_probabilities must contain exactly the three H5 labels"
        )
    return probabilities


def normalize_bearing_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "result_id",
        "device_id",
        "task_id",
        "bearing_id",
        "sender_id",
        "edge_node_id",
        "decision_round_id",
        "window_start_sequence",
        "window_end_sequence",
        "bearing_state",
        "risk_level",
        "action_grade",
        "recommended_action",
        "confidence",
        "data_quality_score",
        "model_version",
        "created_at_ns",
    )
    missing = [field for field in required if payload.get(field) is None]
    if missing:
        raise ValueError(f"missing bearing-result fields: {', '.join(missing)}")

    result = {field: payload[field] for field in required}
    for field in (
        "result_id",
        "device_id",
        "task_id",
        "bearing_id",
        "sender_id",
        "edge_node_id",
        "decision_round_id",
        "risk_level",
        "recommended_action",
        "model_version",
    ):
        result[field] = str(result[field]).strip()
        if not result[field]:
            raise ValueError(f"{field} must not be empty")

    if result["risk_level"] not in RISK_LEVELS:
        raise ValueError("risk_level must be low, medium, or high")

    # Every non-binary state - including an empty one - is rejected with the
    # same message so senders see one stable contract error.
    result["bearing_state"] = str(result["bearing_state"]).strip()
    if result["bearing_state"] not in BINARY_BEARING_STATES:
        raise ValueError("bearing_state must be normal or fault")

    result["window_start_sequence"] = int(result["window_start_sequence"])
    result["window_end_sequence"] = int(result["window_end_sequence"])
    if result["window_start_sequence"] <= 0:
        raise ValueError("window_start_sequence must be positive")
    if result["window_end_sequence"] < result["window_start_sequence"]:
        raise ValueError("window_end_sequence must not precede window_start_sequence")

    result["action_grade"] = int(result["action_grade"])
    if result["action_grade"] not in ACTION_BY_GRADE:
        raise ValueError("action_grade must be between 0 and 4")
    if result["recommended_action"] != ACTION_BY_GRADE[result["action_grade"]]:
        raise ValueError("recommended_action does not match action_grade")

    for field in ("confidence", "data_quality_score"):
        if isinstance(result[field], bool):
            raise ValueError(f"{field} must be numeric")
        result[field] = float(result[field])
        if not 0.0 <= result[field] <= 1.0:
            raise ValueError(f"{field} must be between 0 and 1")

    result["created_at_ns"] = int(result["created_at_ns"])
    if result["created_at_ns"] < 0:
        raise ValueError("created_at_ns must be non-negative")

    # Audit-only fields from the raw model output; never used for conflict checks.
    result["run_id"] = _normalize_run_id(payload)
    diagnosis_label = payload.get("diagnosis_label")
    if diagnosis_label is not None:
        diagnosis_label = str(diagnosis_label).strip()
        if not diagnosis_label:
            diagnosis_label = None
    if diagnosis_label is not None:
        result["diagnosis_label"] = diagnosis_label

    # Scoring runs here, inside normalize_bearing_result, so the persisted
    # bearing JSON already carries the complete action_scorer_v1 trace. The raw
    # class_probabilities are preserved verbatim; the scorer stores the
    # renormalized copy as normalized_class_probabilities.
    class_probabilities = _require_class_probabilities(payload)
    result["class_probabilities"] = class_probabilities
    scored = score_bearing_action(
        class_probabilities,
        risk_level=result["risk_level"],
        data_quality_score=result["data_quality_score"],
    )
    result.update(scored)

    result["summary_window_id"] = build_summary_window_id(
        result["device_id"],
        result["run_id"],
        result["window_start_sequence"],
        result["window_end_sequence"],
    )
    return result


def group_key(result: Mapping[str, Any]) -> str:
    """Grouping key of a bearing result: the shared summary window identity."""

    return str(result["summary_window_id"])
