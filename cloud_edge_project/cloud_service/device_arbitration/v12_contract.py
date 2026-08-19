"""Adapt the V1.2 device-arbitration contract to the retained bearing rules."""

from __future__ import annotations

from typing import Any, Mapping

from core.arbitration_contracts import ArbitrationValidationError
from core.bearing_actions import ACTION_TO_STATE, action_for_grade


_IDENTITY_FIELDS = (
    "conflict_id",
    "device_id",
    "task_id",
    "decision_round_id",
    "device_result_revision",
    "bearing_result_ids",
    "bearing_results",
    "comparison",
    "local_arbitration_supported",
)


def is_v12_device_arbitration_request(payload: Mapping[str, Any]) -> bool:
    return any(field in payload for field in _IDENTITY_FIELDS[3:])


def adapt_v12_device_arbitration_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate V1.2 identity and produce the existing bearing-rule input."""

    missing = [field for field in _IDENTITY_FIELDS if field not in payload]
    if missing:
        raise ArbitrationValidationError(
            "INVALID_REQUEST", f"missing V1.2 device arbitration fields: {missing}"
        )
    device_id = _text(payload["device_id"], "device_id")
    task_id = _text(payload["task_id"], "task_id")
    conflict_id = _text(payload["conflict_id"], "conflict_id")
    decision_round_id = _text(payload["decision_round_id"], "decision_round_id")
    revision = _positive_int(payload["device_result_revision"], "device_result_revision")
    result_ids = _text_list(payload["bearing_result_ids"], "bearing_result_ids")
    results = payload["bearing_results"]
    if not isinstance(results, list) or not results:
        _invalid("bearing_results must be a non-empty array")
    if not isinstance(payload["comparison"], Mapping):
        _invalid("comparison must be an object")
    if not isinstance(payload["local_arbitration_supported"], bool):
        _invalid("local_arbitration_supported must be a boolean")

    scenario_results: list[dict[str, Any]] = []
    seen_bearings: set[str] = set()
    actual_ids: list[str] = []
    for value in results:
        if not isinstance(value, Mapping):
            _invalid("each bearing result must be an object")
        bearing_id = _text(value.get("bearing_id"), "bearing_id")
        if bearing_id in seen_bearings:
            _invalid("bearing_id values must be unique")
        seen_bearings.add(bearing_id)
        actual_ids.append(_text(value.get("bearing_result_id"), "bearing_result_id"))
        confidence = _score(value.get("confidence"), "confidence")
        risk_level = _enum(value.get("risk_level"), "risk_level", {"LOW", "MEDIUM", "HIGH"})
        action_grade = _action_grade(value.get("action_level"))
        data_quality_score = _score(
            value.get("data_quality_score", confidence), "data_quality_score"
        )
        scenario_results.append(
            {
                "bearing_id": bearing_id,
                "bearing_state": ACTION_TO_STATE[action_for_grade(action_grade)],
                "confidence": confidence,
                "data_quality_score": data_quality_score,
                "risk_level": risk_level.lower(),
                "recommended_action": action_for_grade(action_grade),
            }
        )
    if result_ids != actual_ids:
        _invalid("bearing_result_ids must match bearing_results in order")

    return {
        "scenario_type": "bearing",
        "conflict_id": conflict_id,
        "subject_id": device_id,
        "task_id": task_id,
        "scenario_payload": {"bearing_results": scenario_results},
        "v12_identity": {
            "device_id": device_id,
            "task_id": task_id,
            "decision_round_id": decision_round_id,
            "device_result_revision": revision,
            "bearing_result_ids": result_ids,
        },
    }


def attach_v12_identity(result: Mapping[str, Any], adapted: Mapping[str, Any]) -> dict[str, Any]:
    identity = adapted["v12_identity"]
    return {
        **result,
        "device_id": identity["device_id"],
        "task_id": identity["task_id"],
        "decision_round_id": identity["decision_round_id"],
        "device_result_revision": identity["device_result_revision"],
        "bearing_result_ids": identity["bearing_result_ids"],
    }


def _invalid(message: str) -> None:
    raise ArbitrationValidationError("INVALID_REQUEST", message)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{field} is required")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid(f"{field} must be a positive integer")
    return value


def _text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _invalid(f"{field} must be a non-empty array")
    result = [_text(item, field) for item in value]
    if len(set(result)) != len(result):
        _invalid(f"{field} must be unique")
    return result


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"{field} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        _invalid(f"{field} must be in [0, 1]")
    return result


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    text = _text(value, field).upper()
    if text not in allowed:
        _invalid(f"{field} is not supported")
    return text


def _action_grade(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in range(5):
        _invalid("action_level must be an integer from 0 to 4")
    return value
