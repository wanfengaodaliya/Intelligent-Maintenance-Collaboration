"""Validation for the high-sample-rate cloud review request."""

from __future__ import annotations

from math import isfinite
from typing import Any

from cloud_service.perception.contracts import ValidationResult
from common.schemas import ContractError


_TOP_LEVEL_FIELDS = {"edge_perception_result", "cloud_raw_packet"}
_IDENTITY_FIELDS = ("task_id", "packet_id", "device_id")
_SIGNAL_FIELDS = ("vibration", "phase_current_1", "phase_current_2")
_WINDOW_TOLERANCE_NS = 100_000


def validate_cloud_review_quality(payload: dict[str, Any]) -> ValidationResult:
    """Return all independent blocking and non-blocking quality findings."""

    blocking: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ValidationResult(False, ["INVALID_TYPE: CloudReviewRequest must be an object"], warnings)

    unexpected = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unexpected:
        blocking.append("INVALID_CLOUD_REVIEW_REQUEST: unexpected top-level fields: " + ", ".join(unexpected))
    missing = sorted(_TOP_LEVEL_FIELDS - set(payload))
    if missing:
        blocking.append("MISSING_FIELD: " + ", ".join(missing))

    edge = _mapping(payload.get("edge_perception_result"), "edge_perception_result", blocking)
    raw = _mapping(payload.get("cloud_raw_packet"), "cloud_raw_packet", blocking)
    if edge is None or raw is None:
        return ValidationResult(not blocking, blocking, warnings)

    packet_id = _validate_identity(edge, raw, blocking)
    _validate_operating_context(edge, packet_id, blocking)
    _validate_window(edge, raw, packet_id, blocking)
    _validate_signals(raw, packet_id, blocking, warnings)
    warnings.append("SENSOR_RANGE_UNKNOWN: no sensor range configuration is supplied")
    return ValidationResult(not blocking, blocking, warnings)


def validate_cloud_review_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a CloudReviewRequest or raise the project's ContractError."""

    result = validate_cloud_review_quality(payload)
    if not result.valid:
        packet_id = _packet_id(payload)
        raise ContractError("INVALID_CLOUD_REVIEW_REQUEST", "; ".join(result.blocking_issues), packet_id)
    return payload


def _mapping(value: Any, field: str, blocking: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        blocking.append(f"INVALID_TYPE: {field} must be an object")
        return None
    return value


def _validate_identity(edge: dict[str, Any], raw: dict[str, Any], blocking: list[str]) -> str | None:
    packet_id: str | None = None
    for field in _IDENTITY_FIELDS:
        edge_value = edge.get(field)
        raw_value = raw.get(field)
        if not isinstance(edge_value, str) or not edge_value.strip():
            blocking.append(f"INVALID_TYPE: edge_perception_result.{field} must be a non-empty string")
        if not isinstance(raw_value, str) or not raw_value.strip():
            blocking.append(f"INVALID_TYPE: cloud_raw_packet.{field} must be a non-empty string")
        if isinstance(edge_value, str) and isinstance(raw_value, str) and edge_value != raw_value:
            blocking.append(f"IDENTIFIER_MISMATCH: {field} differs between edge_perception_result and cloud_raw_packet")
        if field == "packet_id" and isinstance(raw_value, str) and raw_value.strip():
            packet_id = raw_value

    edge_sequence = _positive_int(edge.get("sequence_number"), "edge_perception_result.sequence_number", blocking)
    raw_sequence = _positive_int(raw.get("sequence_number"), "cloud_raw_packet.sequence_number", blocking)
    if edge_sequence is not None and raw_sequence is not None and edge_sequence != raw_sequence:
        blocking.append("IDENTIFIER_MISMATCH: sequence_number differs between edge_perception_result and cloud_raw_packet")
    return packet_id


def _validate_operating_context(edge: dict[str, Any], packet_id: str | None, blocking: list[str]) -> None:
    context = _mapping(edge.get("operating_context"), "edge_perception_result.operating_context", blocking)
    if context is None:
        return
    for field in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n", "bearing_module_temperature_c"):
        value = _finite_number(context.get(field), f"edge_perception_result.operating_context.{field}", blocking)
        if field in {"shaft_speed_rpm", "bearing_radial_load_n"} and value is not None and value < 0:
            blocking.append(f"INVALID_OPERATING_CONTEXT: {field} must be non-negative")


def _validate_window(edge: dict[str, Any], raw: dict[str, Any], packet_id: str | None, blocking: list[str]) -> None:
    timestamp = _positive_int(edge.get("timestamp_ns"), "edge_perception_result.timestamp_ns", blocking)
    start_ns = _positive_int(raw.get("start_timestamp_ns"), "cloud_raw_packet.start_timestamp_ns", blocking)
    end_ns = _positive_int(raw.get("end_timestamp_ns"), "cloud_raw_packet.end_timestamp_ns", blocking)
    if timestamp is not None and end_ns is not None and timestamp != end_ns:
        blocking.append("TIMESTAMP_MISMATCH: edge_perception_result.timestamp_ns must equal cloud_raw_packet.end_timestamp_ns")
    if start_ns is not None and end_ns is not None and end_ns <= start_ns:
        blocking.append("INVALID_TIMESTAMP: cloud_raw_packet start_timestamp_ns must be before end_timestamp_ns")


def _validate_signals(raw: dict[str, Any], packet_id: str | None, blocking: list[str], warnings: list[str]) -> None:
    signals = _mapping(raw.get("signals"), "cloud_raw_packet.signals", blocking)
    if signals is None:
        return
    sample_rate: int | None = None
    sample_count: int | None = None
    window_ns = _window_ns(raw)
    for name in _SIGNAL_FIELDS:
        signal = _mapping(signals.get(name), f"cloud_raw_packet.signals.{name}", blocking)
        if signal is None:
            continue
        rate = _positive_int(signal.get("sample_rate_hz"), f"cloud_raw_packet.signals.{name}.sample_rate_hz", blocking)
        count = _positive_int(signal.get("sample_count"), f"cloud_raw_packet.signals.{name}.sample_count", blocking)
        values = signal.get("values")
        if not isinstance(values, list) or any(isinstance(item, list) for item in values):
            blocking.append(f"INVALID_SIGNAL_SHAPE: cloud_raw_packet.signals.{name}.values must be a one-dimensional array")
            continue
        if count is not None and len(values) != count:
            blocking.append(f"SIGNAL_LENGTH_MISMATCH: cloud_raw_packet.signals.{name}.values length must equal sample_count")
        numeric_values: list[float] = []
        for index, item in enumerate(values):
            if not isinstance(item, (int, float)) or isinstance(item, bool) or not isfinite(float(item)):
                blocking.append(f"NONFINITE_VALUE: cloud_raw_packet.signals.{name}.values[{index}] must be finite")
            else:
                numeric_values.append(float(item))
        if numeric_values and sum(numeric_values) / len(numeric_values) != 0.0:
            warnings.append(f"DC_OFFSET_PRESENT: cloud_raw_packet.signals.{name}")
        if rate is not None and sample_rate is not None and rate != sample_rate:
            blocking.append("INVALID_SAMPLE_CONFIG: all signal sample_rate_hz values must match")
        if count is not None and sample_count is not None and count != sample_count:
            blocking.append("INVALID_SAMPLE_CONFIG: all signal sample_count values must match")
        sample_rate = rate if sample_rate is None else sample_rate
        sample_count = count if sample_count is None else sample_count
    if sample_rate is not None and sample_count is not None and window_ns is not None:
        expected_window_ns = sample_count * 1_000_000_000 / sample_rate
        if abs(expected_window_ns - window_ns) > _WINDOW_TOLERANCE_NS:
            blocking.append("INVALID_SAMPLE_CONFIG: sample duration differs from timestamp window by more than 100000 ns")


def _positive_int(value: Any, field: str, blocking: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        blocking.append(f"INVALID_TYPE: {field} must be a positive integer")
        return None
    return value


def _finite_number(value: Any, field: str, blocking: list[str]) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(float(value)):
        blocking.append(f"NONFINITE_VALUE: {field} must be a finite number")
        return None
    return float(value)


def _window_ns(raw: dict[str, Any]) -> int | None:
    start_ns = raw.get("start_timestamp_ns")
    end_ns = raw.get("end_timestamp_ns")
    if not isinstance(start_ns, int) or isinstance(start_ns, bool):
        return None
    if not isinstance(end_ns, int) or isinstance(end_ns, bool) or end_ns <= start_ns:
        return None
    return end_ns - start_ns


def _packet_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("cloud_raw_packet")
    if not isinstance(raw, dict):
        return None
    packet_id = raw.get("packet_id")
    return packet_id if isinstance(packet_id, str) and packet_id.strip() else None
