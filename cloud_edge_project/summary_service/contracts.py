from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


EXPECTED_BEARING_IDS = ("bearing_01", "bearing_02")

ACTION_BY_GRADE = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


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
        "bearing_state",
        "risk_level",
        "recommended_action",
        "model_version",
    ):
        result[field] = str(result[field]).strip()
        if not result[field]:
            raise ValueError(f"{field} must not be empty")

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
        result[field] = float(result[field])
        if not 0.0 <= result[field] <= 1.0:
            raise ValueError(f"{field} must be between 0 and 1")

    result["created_at_ns"] = int(result["created_at_ns"])
    if result["created_at_ns"] < 0:
        raise ValueError("created_at_ns must be non-negative")
    return result


def group_key(result: Mapping[str, Any]) -> tuple[str, int, int]:
    return (
        str(result["device_id"]),
        int(result["window_start_sequence"]),
        int(result["window_end_sequence"]),
    )
