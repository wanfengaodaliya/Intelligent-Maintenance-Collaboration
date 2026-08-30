"""Compatibility shim for scenario-owned bearing packet construction."""

from compatibility.bearing_v12.ingestion_exports import (
    ARRAY_SIGNALS,
    BEARING_ID_PATTERN,
    SENDER_ID_PATTERN,
    TASK_ID_PATTERN,
    TEMPERATURE_SIGNAL,
    WIRE_MAGIC,
    PacketValidationError,
    build_sensor_packet,
    serialize_packet,
)

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
