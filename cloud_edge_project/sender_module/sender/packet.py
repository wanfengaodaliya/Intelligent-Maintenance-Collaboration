from __future__ import annotations

import json
import re
from typing import Any


class PacketValidationError(ValueError):
    pass


ARRAY_SIGNALS = {
    "vibration": 64000,
    "phase_current_1_A": 64000,
    "phase_current_2_A": 64000,
    "shaft_speed_rpm": 4000,
    "load_torque_nm": 4000,
    "bearing_radial_load_n": 4000,
}
TEMPERATURE_SIGNAL = "bearing_module_temperature_c"
TASK_ID_PATTERN = re.compile(r"^task_(\d{5,})$")
BEARING_ID_PATTERN = re.compile(r"^bearing_\d{2,}$")


def _validate_data(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise PacketValidationError("data must be an object")

    for name, expected_rate in ARRAY_SIGNALS.items():
        if name not in data:
            raise PacketValidationError(f"missing signal: {name}")
        signal = data[name]
        if not isinstance(signal, dict):
            raise PacketValidationError(f"{name} must be an object")
        values = signal.get("values")
        if not isinstance(values, list):
            raise PacketValidationError(f"{name}.values must be an array")
        if not values:
            raise PacketValidationError(f"{name}.values cannot be empty")
        if signal.get("sample_count") != len(values):
            raise PacketValidationError(f"{name}.sample_count does not match values")
        if signal.get("sample_rate_hz") != expected_rate:
            raise PacketValidationError(f"{name}.sample_rate_hz must be {expected_rate}")

    temperature = data.get(TEMPERATURE_SIGNAL)
    if not isinstance(temperature, (int, float)):
        raise PacketValidationError("bearing_module_temperature_c must be numeric")


def build_sensor_packet(
    *,
    device_id: str,
    task_id: str,
    bearing_id: str,
    sender_id: str,
    sequence_number: int,
    data: dict[str, Any],
    end_generate_timestamp_ns: int,
) -> dict[str, Any]:
    match = TASK_ID_PATTERN.fullmatch(task_id)
    if not match:
        raise PacketValidationError("task_id must match task_<number>")
    if not isinstance(device_id, str) or not device_id.strip():
        raise PacketValidationError("device_id cannot be empty")
    if not isinstance(bearing_id, str) or not BEARING_ID_PATTERN.fullmatch(bearing_id):
        raise PacketValidationError("bearing_id must match bearing_<number>")
    if not sender_id:
        raise PacketValidationError("sender_id cannot be empty")
    if not 1 <= sequence_number <= 999:
        raise PacketValidationError("sequence_number must be between 1 and 999")
    if end_generate_timestamp_ns <= 0:
        raise PacketValidationError("end_generate_timestamp_ns must be positive")
    _validate_data(data)

    return {
        "device_id": device_id.strip(),
        "task_id": task_id,
        "bearing_id": bearing_id,
        "packet_id": f"{task_id}_{bearing_id}_pkt_{sequence_number:03d}",
        "sender_id": sender_id,
        "sequence_number": sequence_number,
        "end_generate_timestamp_ns": end_generate_timestamp_ns,
        "data": data,
    }


def serialize_packet(packet: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            packet,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PacketValidationError(f"packet cannot be serialized: {exc}") from exc
    return text.encode("utf-8")
