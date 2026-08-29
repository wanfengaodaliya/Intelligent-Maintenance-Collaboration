from __future__ import annotations

import json
import re
import struct
from typing import Any

import numpy as np


class PacketValidationError(ValueError):
    pass


ARRAY_SIGNALS = {
    "vibration": (64000, "mm/s"),
    "phase_current_1_A": (64000, "A"),
    "phase_current_2_A": (64000, "A"),
    "shaft_speed_rpm": (4000, None),
    "load_torque_nm": (4000, None),
    "bearing_radial_load_n": (4000, None),
}
TEMPERATURE_SIGNAL = "bearing_module_temperature_c"
WIRE_MAGIC = b"IMC1"
TASK_ID_PATTERN = re.compile(r"^sd_(\d{2,})_tk_(\d{4,})$")
SENDER_ID_PATTERN = re.compile(r"^sender_(\d{2,})$")
BEARING_ID_PATTERN = re.compile(r"^bearing_\d{2,}$")


def _validate_data(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise PacketValidationError("data must be an object")

    for name, (expected_rate, expected_unit) in ARRAY_SIGNALS.items():
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
        if expected_unit is not None and signal.get("unit") != expected_unit:
            raise PacketValidationError(f"{name}.unit must be {expected_unit}")

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
    run_id: str | None = None,
) -> dict[str, Any]:
    match = TASK_ID_PATTERN.fullmatch(task_id)
    if not match:
        raise PacketValidationError("task_id must match sd_<sender>_tk_<number>")
    if not isinstance(device_id, str) or not device_id.strip():
        raise PacketValidationError("device_id cannot be empty")
    if not isinstance(bearing_id, str) or not BEARING_ID_PATTERN.fullmatch(bearing_id):
        raise PacketValidationError("bearing_id must match bearing_<number>")
    sender_match = SENDER_ID_PATTERN.fullmatch(sender_id)
    if not sender_match:
        raise PacketValidationError("sender_id must match sender_<number>")
    if sender_match.group(1) != match.group(1):
        raise PacketValidationError("task_id does not belong to sender_id")
    if not 1 <= sequence_number <= 999:
        raise PacketValidationError("sequence_number must be between 1 and 999")
    if end_generate_timestamp_ns <= 0:
        raise PacketValidationError("end_generate_timestamp_ns must be positive")
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise PacketValidationError("run_id must be a non-empty string when present")
        if len(run_id) > 128:
            raise PacketValidationError("run_id must not exceed 128 characters")
    _validate_data(data)

    return {
        "device_id": device_id.strip(),
        "task_id": task_id,
        "bearing_id": bearing_id,
        "packet_id": f"{task_id}_{bearing_id}_pkt_{sequence_number:03d}",
        "sender_id": sender_id,
        "sequence_number": sequence_number,
        "run_id": run_id,
        "end_generate_timestamp_ns": end_generate_timestamp_ns,
        "data": data,
    }


def serialize_packet(packet: dict[str, Any]) -> bytes:
    try:
        header = {key: value for key, value in packet.items() if key != "data"}
        data = packet["data"]
        header_data = {TEMPERATURE_SIGNAL: data[TEMPERATURE_SIGNAL]}
        binary_parts: list[bytes] = []
        offset = 0
        for name in ARRAY_SIGNALS:
            signal = data[name]
            source_values = np.asarray(signal["values"], dtype=np.float64)
            if source_values.ndim != 1 or len(source_values) != signal["sample_count"]:
                raise ValueError(
                    f"{name}.values must be a one-dimensional sample array"
                )
            values = source_values.astype("<f4")
            if not np.all(np.isfinite(source_values)) or not np.all(
                np.isfinite(values)
            ):
                raise ValueError(f"{name}.values must contain finite float32 values")
            raw_values = values.tobytes(order="C")
            header_data[name] = {
                key: value for key, value in signal.items() if key != "values"
            }
            header_data[name]["binary"] = {
                "dtype": "float32-le",
                "offset": offset,
                "byte_length": len(raw_values),
            }
            binary_parts.append(raw_values)
            offset += len(raw_values)
        header["data"] = header_data
        header_bytes = json.dumps(
            header,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (KeyError, OverflowError, TypeError, ValueError) as exc:
        raise PacketValidationError(f"packet cannot be serialized: {exc}") from exc
    return WIRE_MAGIC + struct.pack("<I", len(header_bytes)) + header_bytes + b"".join(binary_parts)


__all__ = [
    "PacketValidationError",
    "ARRAY_SIGNALS",
    "TEMPERATURE_SIGNAL",
    "WIRE_MAGIC",
    "TASK_ID_PATTERN",
    "SENDER_ID_PATTERN",
    "BEARING_ID_PATTERN",
    "build_sensor_packet",
    "serialize_packet",
]
