"""Validation for raw-context request responses and upload batches."""

from __future__ import annotations

from math import isfinite
import re
from typing import Any

from common.schemas import (
    ContractError,
    require_field,
    require_int,
    require_mapping,
    require_non_empty_string,
)


_EDGE_CONTEXT_STATUSES = {
    "pending_context",
    "complete",
    "insufficient_context",
}
_HIGH_RATE_SIGNALS = {
    "vibration": ("mm/s", 64_000, 3_200),
    "phase_current_1_A": ("A", 64_000, 3_200),
    "phase_current_2_A": ("A", 64_000, 3_200),
}
_CONTEXT_SIGNALS = {
    "shaft_speed_rpm": (4_000, 200),
    "load_torque_nm": (4_000, 200),
    "bearing_radial_load_n": (4_000, 200),
}
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def validate_edge_context_response(
    payload: Any,
    *,
    request_id: str,
    anchor_packet_id: str,
    before_packet_count: int,
    after_packet_count: int,
) -> dict[str, Any]:
    try:
        return _validate_edge_context_response(
            payload,
            request_id=request_id,
            anchor_packet_id=anchor_packet_id,
            before_packet_count=before_packet_count,
            after_packet_count=after_packet_count,
        )
    except ContractError as error:
        if error.code == "EDGE_REJECTED_CONTEXT_REQUEST":
            raise
        raise ContractError(
            "EDGE_REJECTED_CONTEXT_REQUEST",
            error.message,
            error.packet_id,
        ) from error


def _validate_edge_context_response(
    payload: Any,
    *,
    request_id: str,
    anchor_packet_id: str,
    before_packet_count: int,
    after_packet_count: int,
) -> dict[str, Any]:
    response = require_mapping(payload, "RawContextResponse")
    if require_non_empty_string(
        require_field(response, "request_id"), "request_id"
    ) != request_id:
        raise ContractError(
            "EDGE_REJECTED_CONTEXT_REQUEST",
            "edge response request_id does not match request",
        )
    if require_non_empty_string(
        require_field(response, "anchor_packet_id"), "anchor_packet_id"
    ) != anchor_packet_id:
        raise ContractError(
            "EDGE_REJECTED_CONTEXT_REQUEST",
            "edge response anchor_packet_id does not match request",
        )
    if response.get("status") not in _EDGE_CONTEXT_STATUSES:
        raise ContractError(
            "EDGE_REJECTED_CONTEXT_REQUEST",
            "edge response status is invalid",
        )
    expected_counts = {
        "before_context": before_packet_count,
        "after_context": after_packet_count,
    }
    for field, requested_count in expected_counts.items():
        context = require_mapping(require_field(response, field), field)
        expected_count = require_int(
            require_field(context, "expected_count"), f"{field}.expected_count"
        )
        available_count = require_int(
            require_field(context, "available_count"), f"{field}.available_count"
        )
        if (
            expected_count != requested_count
            or not 0 <= available_count <= expected_count
        ):
            raise ContractError(
                "EDGE_REJECTED_CONTEXT_REQUEST",
                f"{field} counts are invalid",
            )
        require_non_empty_string(
            require_field(context, "upload_status"), f"{field}.upload_status"
        )
        missing = require_field(context, "missing_sequence_numbers")
        if not isinstance(missing, list) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in missing
        ):
            raise ContractError(
                "EDGE_REJECTED_CONTEXT_REQUEST",
                f"{field}.missing_sequence_numbers is invalid",
            )
    return response


def validate_raw_context_batch_envelope(payload: Any) -> dict[str, Any]:
    try:
        return _validate_raw_context_batch_envelope(payload)
    except ContractError as error:
        if error.code == "INVALID_CONTEXT_BATCH":
            raise
        raise ContractError(
            "INVALID_CONTEXT_BATCH",
            error.message,
            error.packet_id,
        ) from error


def _validate_raw_context_batch_envelope(payload: Any) -> dict[str, Any]:
    batch = require_mapping(payload, "RawContextBatch")
    for field in (
        "batch_id",
        "request_id",
        "task_id",
        "sender_id",
        "anchor_packet_id",
    ):
        require_non_empty_string(require_field(batch, field), field)
    for field in (
        "anchor_sequence_number",
        "first_sequence_number",
        "last_sequence_number",
        "item_count",
        "sent_at_ns",
    ):
        value = require_int(require_field(batch, field), field)
        if value <= 0:
            raise ContractError(
                "INVALID_CONTEXT_BATCH", f"{field} must be positive"
            )
    if batch.get("context_position") not in {"before", "after"}:
        raise ContractError(
            "INVALID_CONTEXT_BATCH",
            "context_position must be before or after",
        )
    if batch.get("context_status") not in _EDGE_CONTEXT_STATUSES:
        raise ContractError(
            "INVALID_CONTEXT_BATCH", "context_status is invalid"
        )
    packets = require_field(batch, "packets")
    item_count = batch["item_count"]
    if (
        not isinstance(packets, list)
        or not 1 <= item_count <= 10
        or len(packets) != item_count
    ):
        raise ContractError(
            "INVALID_CONTEXT_BATCH",
            "item_count must equal 1 to 10 packets",
        )
    if (
        batch["last_sequence_number"]
        - batch["first_sequence_number"]
        + 1
        != item_count
    ):
        raise ContractError(
            "INVALID_CONTEXT_BATCH",
            "declared sequence range must match item_count",
        )
    missing = require_field(batch, "missing_sequence_numbers")
    if not isinstance(missing, list) or any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        for value in missing
    ):
        raise ContractError(
            "INVALID_CONTEXT_BATCH",
            "missing_sequence_numbers must contain positive integers",
        )
    return batch


def validate_raw_context_packet(
    payload: Any,
    *,
    batch: dict[str, Any],
    anchor_end_timestamp_ns: int,
    before_packet_count: int,
    after_packet_count: int,
) -> dict[str, Any]:
    try:
        return _validate_raw_context_packet(
            payload,
            batch=batch,
            anchor_end_timestamp_ns=anchor_end_timestamp_ns,
            before_packet_count=before_packet_count,
            after_packet_count=after_packet_count,
        )
    except ContractError as error:
        if error.code in {
            "INVALID_CONTEXT_PACKET",
            "INVALID_SAMPLE_CONFIG",
            "INVALID_SIGNAL_SHAPE",
            "NONFINITE_VALUE",
            "TIMESTAMP_MISMATCH",
            "CONTEXT_SEQUENCE_OUT_OF_RANGE",
        }:
            raise
        raise ContractError(
            "INVALID_CONTEXT_PACKET",
            error.message,
            error.packet_id,
        ) from error


def _validate_raw_context_packet(
    payload: Any,
    *,
    batch: dict[str, Any],
    anchor_end_timestamp_ns: int,
    before_packet_count: int,
    after_packet_count: int,
) -> dict[str, Any]:
    packet = require_mapping(payload, "cloud_raw_packet")
    packet_id = (
        packet.get("packet_id")
        if isinstance(packet.get("packet_id"), str)
        else None
    )
    require_non_empty_string(
        require_field(packet, "packet_id", packet_id), "packet_id", packet_id
    )
    if _SAFE_IDENTIFIER.fullmatch(packet["packet_id"]) is None:
        raise ContractError(
            "INVALID_CONTEXT_PACKET",
            "packet_id contains unsupported characters",
            packet_id,
        )
    for field in ("task_id", "sender_id"):
        supplied = packet.get(field)
        if supplied is not None and supplied != batch[field]:
            raise ContractError(
                "INVALID_CONTEXT_PACKET",
                "packet identity does not match batch",
                packet_id,
            )
        packet[field] = batch[field]
    sequence_number = require_int(
        require_field(packet, "sequence_number", packet_id),
        "sequence_number",
        packet_id,
    )
    if (
        sequence_number < batch["first_sequence_number"]
        or sequence_number > batch["last_sequence_number"]
        or sequence_number == batch["anchor_sequence_number"]
    ):
        raise ContractError(
            "CONTEXT_SEQUENCE_OUT_OF_RANGE",
            "packet sequence is outside the declared context range",
            packet_id,
        )
    anchor = batch["anchor_sequence_number"]
    if batch["context_position"] == "before":
        allowed = range(anchor - before_packet_count, anchor)
    else:
        allowed = range(anchor + 1, anchor + after_packet_count + 1)
    if sequence_number not in allowed:
        raise ContractError(
            "CONTEXT_SEQUENCE_OUT_OF_RANGE",
            "packet sequence is outside requested context",
            packet_id,
        )
    end_timestamp = require_int(
        require_field(packet, "end_generate_timestamp_ns", packet_id),
        "end_generate_timestamp_ns",
        packet_id,
    )
    if end_timestamp <= 0:
        raise ContractError(
            "INVALID_CONTEXT_PACKET",
            "end_generate_timestamp_ns must be positive",
            packet_id,
        )
    expected_end_timestamp = anchor_end_timestamp_ns + (
        sequence_number - batch["anchor_sequence_number"]
    ) * 50_000_000
    if end_timestamp != expected_end_timestamp:
        raise ContractError(
            "TIMESTAMP_MISMATCH",
            "packet timestamp does not connect to anchor packet",
            packet_id,
        )
    data = require_mapping(
        require_field(packet, "data", packet_id), "data", packet_id
    )
    for name, (unit, rate, count) in _HIGH_RATE_SIGNALS.items():
        _validate_signal(
            data,
            name=name,
            rate=rate,
            count=count,
            packet_id=packet_id,
            unit=unit,
        )
    for name, (rate, count) in _CONTEXT_SIGNALS.items():
        values = _validate_signal(
            data,
            name=name,
            rate=rate,
            count=count,
            packet_id=packet_id,
        )
        if name in {"shaft_speed_rpm", "bearing_radial_load_n"} and any(
            value < 0 for value in values
        ):
            raise ContractError(
                "INVALID_CONTEXT_PACKET",
                f"{name} values must be non-negative",
                packet_id,
            )
    _finite_number(
        data.get("bearing_module_temperature_c"),
        "bearing_module_temperature_c",
        packet_id,
    )
    return packet


def _validate_signal(
    data: dict[str, Any],
    *,
    name: str,
    rate: int,
    count: int,
    packet_id: str | None,
    unit: str | None = None,
) -> list[float]:
    signal = require_mapping(
        require_field(data, name, packet_id), name, packet_id
    )
    if unit is not None and signal.get("unit") != unit:
        raise ContractError(
            "INVALID_SAMPLE_CONFIG", f"{name}.unit is invalid", packet_id
        )
    if (
        signal.get("sample_rate_hz") != rate
        or signal.get("sample_count") != count
    ):
        raise ContractError(
            "INVALID_SAMPLE_CONFIG",
            f"{name} sample configuration is invalid",
            packet_id,
        )
    values = signal.get("values")
    if (
        not isinstance(values, list)
        or any(isinstance(value, list) for value in values)
        or len(values) != count
    ):
        raise ContractError(
            "INVALID_SIGNAL_SHAPE",
            f"{name}.values shape is invalid",
            packet_id,
        )
    return [
        _finite_number(value, f"{name}.values[{index}]", packet_id)
        for index, value in enumerate(values)
    ]


def _finite_number(
    value: Any, field: str, packet_id: str | None
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise ContractError(
            "NONFINITE_VALUE", f"{field} must be finite", packet_id
        )
    return float(value)
