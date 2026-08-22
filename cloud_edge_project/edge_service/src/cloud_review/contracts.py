"""Document 7.3 control validation for edge cloud review."""
# 该模块校验边缘云端复核控制消息是否符合既定接口契约。

from __future__ import annotations

from typing import Any, Mapping

from core.bearing_actions import grade_for_action
from core.diagnosis_contracts import CloudBearingResult


class CloudReviewError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def validate_control(payload: Mapping[str, Any]) -> dict[str, Any]:
    top = {
        "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
        "packet_id", "decision_round_id", "diagnosis_window_id",
        "window_start_sequence", "window_end_sequence", "trigger_reasons", "source", "target", "created_at_ns",
    }
    if not isinstance(payload, Mapping) or set(payload) != top:
        raise CloudReviewError("INVALID_CLOUD_REVIEW_TASK", "control fields do not match document 7.3")
    try:
        source = _mapping(payload["source"], "source")
        target = _mapping(payload["target"], "target")
        if set(source) != {"holder_id", "raw_data_ref", "context_ref"}:
            raise ValueError("source fields do not match document 7.3")
        if set(target) != {"cloud_node_id", "endpoint"}:
            raise ValueError("target fields do not match document 7.3")
        reasons = payload["trigger_reasons"]
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("trigger_reasons must be a non-empty array")
        control = {
            "decision_id": _text(payload["decision_id"], "decision_id"),
            "cloud_task_id": _text(payload["cloud_task_id"], "cloud_task_id"),
            "device_id": _text(payload["device_id"], "device_id"),
            "task_id": _safe_id(payload["task_id"], "task_id"),
            "bearing_id": _safe_id(payload["bearing_id"], "bearing_id"),
            "packet_id": _safe_id(payload["packet_id"], "packet_id"),
            "decision_round_id": _safe_id(payload["decision_round_id"], "decision_round_id"),
            "diagnosis_window_id": _safe_id(payload["diagnosis_window_id"], "diagnosis_window_id"),
            "window_start_sequence": _positive_int(payload["window_start_sequence"], "window_start_sequence"),
            "window_end_sequence": _positive_int(payload["window_end_sequence"], "window_end_sequence"),
            "trigger_reasons": [_text(value, "trigger_reason") for value in reasons],
            "source": {
                "holder_id": _safe_id(source["holder_id"], "holder_id"),
                "raw_data_ref": _text(source["raw_data_ref"], "raw_data_ref"),
                "context_ref": _optional_text(source["context_ref"], "context_ref"),
            },
            "target": {
                "cloud_node_id": _safe_id(target["cloud_node_id"], "cloud_node_id"),
                "endpoint": _text(target["endpoint"], "endpoint"),
            },
            "created_at_ns": _positive_int(payload["created_at_ns"], "created_at_ns"),
        }
        if control["window_end_sequence"] < control["window_start_sequence"]:
            raise ValueError("window sequence range must be ordered")
        return control
    except (KeyError, TypeError, ValueError) as error:
        raise CloudReviewError("INVALID_CLOUD_REVIEW_TASK", str(error)) from error


def parse_cloud_bearing_result(payload: Mapping[str, Any]) -> CloudBearingResult:
    """Validate every V1.2 cloud-result field before it may revise edge state.

    云端可在 V1.2 契约字段之外附带信息性字段（如 edge_label 供比对），
    因此这里要求 payload 至少包含全部必填字段（子集校验），而非恰好相等。
    """
    required = {
        "schema_version", "result_id", "review_id", "device_id", "task_id",
        "bearing_id", "sender_id", "decision_round_id", "diagnosis_window_id",
        "window_start_sequence", "window_end_sequence", "window_start_ns", "window_end_ns",
        "bearing_state", "confidence", "data_quality_score", "risk_level", "action_grade",
        "recommended_action", "model_version", "created_at_ns",
    }
    if not isinstance(payload, Mapping) or not required <= set(payload):
        raise CloudReviewError("INVALID_CLOUD_BEARING_RESULT", "cloud bearing-result fields do not match V1.2")
    try:
        if payload["schema_version"] != "cloud-bearing-result/2.0":
            raise ValueError("schema_version must be cloud-bearing-result/2.0")
        action_grade = _bounded_int(payload["action_grade"], "action_grade", 0, 4)
        recommended_action = _text(payload["recommended_action"], "recommended_action")
        if grade_for_action(recommended_action) != action_grade:
            raise ValueError("recommended_action does not match action_grade")
        result = CloudBearingResult(
            result_id=_text(payload["result_id"], "result_id"),
            review_id=_text(payload["review_id"], "review_id"),
            device_id=_text(payload["device_id"], "device_id"),
            task_id=_text(payload["task_id"], "task_id"),
            bearing_id=_text(payload["bearing_id"], "bearing_id"),
            sender_id=_text(payload["sender_id"], "sender_id"),
            decision_round_id=_text(payload["decision_round_id"], "decision_round_id"),
            diagnosis_window_id=_text(payload["diagnosis_window_id"], "diagnosis_window_id"),
            window_start_sequence=_positive_int(payload["window_start_sequence"], "window_start_sequence"),
            window_end_sequence=_positive_int(payload["window_end_sequence"], "window_end_sequence"),
            window_start_ns=_non_negative_int(payload["window_start_ns"], "window_start_ns"),
            window_end_ns=_positive_int(payload["window_end_ns"], "window_end_ns"),
            bearing_state=_text(payload["bearing_state"], "bearing_state"),
            confidence=_bounded_float(payload["confidence"], "confidence", 0.0, 1.0),
            data_quality_score=_bounded_float(payload["data_quality_score"], "data_quality_score", 0.0, 1.0),
            risk_level=_text(payload["risk_level"], "risk_level"),
            action_grade=action_grade,
            recommended_action=recommended_action,
            model_version=_text(payload["model_version"], "model_version"),
            created_at_ns=_positive_int(payload["created_at_ns"], "created_at_ns"),
        )
        if result.window_end_sequence < result.window_start_sequence or result.window_end_ns < result.window_start_ns:
            raise ValueError("cloud bearing-result window range must be ordered")
        return result
    except (TypeError, ValueError) as error:
        raise CloudReviewError("INVALID_CLOUD_BEARING_RESULT", str(error)) from error


def validate_record(raw_packet: Mapping[str, Any], edge_result: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw_packet, Mapping) or not isinstance(edge_result, Mapping):
        raise CloudReviewError("INVALID_CLOUD_REVIEW_RECORD", "raw packet and edge result must be objects")
    raw = dict(raw_packet)
    edge = dict(edge_result)
    for field in ("device_id", "task_id", "bearing_id", "sender_id", "packet_id"):
        raw_value = _text(raw.get(field), f"raw_packet.{field}")
        edge_value = _text(edge.get(field), f"edge_result.{field}")
        if raw_value != edge_value:
            raise CloudReviewError("CLOUD_REVIEW_IDENTITY_MISMATCH", f"{field} differs")
    raw_sequence = _positive_int(raw.get("sequence_number"), "raw_packet.sequence_number")
    edge_sequence = _positive_int(edge.get("sequence_number"), "edge_result.sequence_number")
    if raw_sequence != edge_sequence:
        raise CloudReviewError("CLOUD_REVIEW_IDENTITY_MISMATCH", "sequence_number differs")
    _safe_id(raw["task_id"], "task_id")
    _safe_id(raw["bearing_id"], "bearing_id")
    _safe_id(raw["packet_id"], "packet_id")
    return raw, edge


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _safe_id(value: Any, field: str) -> str:
    text = _text(value, field)
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in text):
        raise ValueError(f"{field} contains unsafe characters")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _bounded_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return normalized
