"""Validation for the sender-originated cloud review request."""

from __future__ import annotations

from math import isfinite
from typing import Any

from cloud_service.perception.contracts import ValidationResult
from common.schemas import ContractError


_TOP_LEVEL_FIELDS = {"edge_perception_result", "cloud_raw_packet"}
_IDENTITY_FIELDS = ("task_id", "packet_id", "sender_id")
_HIGH_RATE_SIGNALS = ("vibration", "phase_current_1_A", "phase_current_2_A")
_CONTEXT_SIGNALS = ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n")
_CONTEXT_STATS = ("mean", "last", "minimum", "maximum", "standard_deviation")
_HIGH_RATE = (64_000, 3_200)
_CONTEXT_RATE = (4_000, 200)


def validate_cloud_review_quality(payload: dict[str, Any]) -> ValidationResult:
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
        return ValidationResult(False, blocking, warnings)
    _validate_identity(edge, raw, blocking)
    _validate_timestamps(edge, raw, blocking)
    _validate_quality(edge.get("perception_quality"), warnings)
    _validate_edge_context(edge, blocking)
    _validate_raw_data(raw, blocking)
    return ValidationResult(not blocking, blocking, warnings)


def validate_cloud_review_request(payload: dict[str, Any]) -> dict[str, Any]:
    result = validate_cloud_review_quality(payload)
    if not result.valid:
        raise ContractError("INVALID_CLOUD_REVIEW_REQUEST", "; ".join(result.blocking_issues), _packet_id(payload))
    return payload


def _mapping(value: Any, name: str, blocking: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        blocking.append(f"INVALID_TYPE: {name} must be an object")
        return None
    return value


def _validate_identity(edge: dict[str, Any], raw: dict[str, Any], blocking: list[str]) -> None:
    for field in _IDENTITY_FIELDS:
        left, right = edge.get(field), raw.get(field)
        if not isinstance(left, str) or not left.strip():
            blocking.append(f"INVALID_TYPE: edge_perception_result.{field} must be a non-empty string")
        if not isinstance(right, str) or not right.strip():
            blocking.append(f"INVALID_TYPE: cloud_raw_packet.{field} must be a non-empty string")
        if isinstance(left, str) and isinstance(right, str) and left != right:
            blocking.append(f"IDENTIFIER_MISMATCH: {field} differs between edge_perception_result and cloud_raw_packet")
    left, right = _positive_int(edge.get("sequence_number"), "edge_perception_result.sequence_number", blocking), _positive_int(raw.get("sequence_number"), "cloud_raw_packet.sequence_number", blocking)
    if left is not None and right is not None and left != right:
        blocking.append("IDENTIFIER_MISMATCH: sequence_number differs between edge_perception_result and cloud_raw_packet")


def _validate_timestamps(edge: dict[str, Any], raw: dict[str, Any], blocking: list[str]) -> None:
    edge_end = _positive_int(edge.get("end_generate_timestamp_ns"), "edge_perception_result.end_generate_timestamp_ns", blocking)
    raw_end = _positive_int(raw.get("end_generate_timestamp_ns"), "cloud_raw_packet.end_generate_timestamp_ns", blocking)
    generated = _positive_int(edge.get("feature_generated_at_ns"), "edge_perception_result.feature_generated_at_ns", blocking)
    if edge_end is not None and raw_end is not None and edge_end != raw_end:
        blocking.append("TIMESTAMP_MISMATCH: end_generate_timestamp_ns differs between edge_perception_result and cloud_raw_packet")
    if edge_end is not None and generated is not None and generated < edge_end:
        blocking.append("TIMESTAMP_MISMATCH: feature_generated_at_ns must not precede end_generate_timestamp_ns")


def _validate_quality(value: Any, warnings: list[str]) -> None:
    if not isinstance(value, dict) or value.get("status") not in {"good", "warning"} or not isinstance(value.get("flags"), list):
        warnings.append("INVALID_EDGE_QUALITY_FORMAT: perception_quality must contain good/warning status and array flags")


def _validate_edge_context(edge: dict[str, Any], blocking: list[str]) -> None:
    features = _mapping(edge.get("features"), "edge_perception_result.features", blocking)
    if features is None:
        return
    context = _mapping(features.get("operating_context"), "edge_perception_result.features.operating_context", blocking)
    if context is None:
        return
    for name in _CONTEXT_SIGNALS:
        statistics = _mapping(context.get(name), f"edge_perception_result.features.operating_context.{name}", blocking)
        if statistics is None:
            continue
        for statistic in _CONTEXT_STATS:
            value = _finite_number(statistics.get(statistic), f"edge_perception_result.features.operating_context.{name}.{statistic}", blocking)
            if name in {"shaft_speed_rpm", "bearing_radial_load_n"} and value is not None and value < 0:
                blocking.append(f"INVALID_OPERATING_CONTEXT: {name}.{statistic} must be non-negative")
    _finite_number(context.get("bearing_module_temperature_c"), "edge_perception_result.features.operating_context.bearing_module_temperature_c", blocking)


def _validate_raw_data(raw: dict[str, Any], blocking: list[str]) -> None:
    data = _mapping(raw.get("data"), "cloud_raw_packet.data", blocking)
    if data is None:
        return
    for name in _HIGH_RATE_SIGNALS:
        _validate_series(data.get(name), f"cloud_raw_packet.data.{name}", _HIGH_RATE, blocking)
    for name in _CONTEXT_SIGNALS:
        values = _validate_series(data.get(name), f"cloud_raw_packet.data.{name}", _CONTEXT_RATE, blocking)
        if values is not None and name in {"shaft_speed_rpm", "bearing_radial_load_n"} and any(value < 0 for value in values):
            blocking.append(f"INVALID_OPERATING_CONTEXT: {name} values must be non-negative")
    _finite_number(data.get("bearing_module_temperature_c"), "cloud_raw_packet.data.bearing_module_temperature_c", blocking)


def _validate_series(value: Any, name: str, configuration: tuple[int, int], blocking: list[str]) -> list[float] | None:
    signal = _mapping(value, name, blocking)
    if signal is None:
        return None
    rate = _positive_int(signal.get("sample_rate_hz"), f"{name}.sample_rate_hz", blocking)
    count = _positive_int(signal.get("sample_count"), f"{name}.sample_count", blocking)
    if (rate, count) != configuration:
        blocking.append(f"INVALID_SAMPLE_CONFIG: {name} must be {configuration[0]} Hz/{configuration[1]} samples")
    values = signal.get("values")
    if not isinstance(values, list) or any(isinstance(item, list) for item in values):
        blocking.append(f"INVALID_SIGNAL_SHAPE: {name}.values must be a one-dimensional array")
        return None
    if count is not None and len(values) != count:
        blocking.append(f"SIGNAL_LENGTH_MISMATCH: {name}.values length must equal sample_count")
    numbers: list[float] = []
    for index, item in enumerate(values):
        number = _finite_number(item, f"{name}.values[{index}]", blocking)
        if number is not None:
            numbers.append(number)
    return numbers


def _positive_int(value: Any, name: str, blocking: list[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        blocking.append(f"INVALID_TYPE: {name} must be a positive integer")
        return None
    return value


def _finite_number(value: Any, name: str, blocking: list[str]) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(float(value)):
        blocking.append(f"NONFINITE_VALUE: {name} must be a finite number")
        return None
    return float(value)


def _packet_id(payload: Any) -> str | None:
    raw = payload.get("cloud_raw_packet") if isinstance(payload, dict) else None
    value = raw.get("packet_id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value.strip() else None
