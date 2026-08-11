"""Document 7.3 control validation for edge cloud review."""
# 该模块校验边缘云端复核控制消息是否符合既定接口契约。

from __future__ import annotations

from typing import Any, Mapping


class CloudReviewError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def validate_control(payload: Mapping[str, Any]) -> dict[str, Any]:
    top = {
        "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
        "packet_id", "trigger_reasons", "source", "target", "created_at_ns",
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
        return {
            "decision_id": _text(payload["decision_id"], "decision_id"),
            "cloud_task_id": _text(payload["cloud_task_id"], "cloud_task_id"),
            "device_id": _text(payload["device_id"], "device_id"),
            "task_id": _safe_id(payload["task_id"], "task_id"),
            "bearing_id": _safe_id(payload["bearing_id"], "bearing_id"),
            "packet_id": _safe_id(payload["packet_id"], "packet_id"),
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
    except (KeyError, TypeError, ValueError) as error:
        raise CloudReviewError("INVALID_CLOUD_REVIEW_TASK", str(error)) from error


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
