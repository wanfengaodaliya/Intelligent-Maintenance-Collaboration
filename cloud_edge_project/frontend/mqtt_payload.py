"""Decode MQTT payloads into the metadata required by the dashboard."""

from __future__ import annotations

import json
import struct
from typing import Any


SENSOR_PACKET_WIRE_MAGIC = b"IMC1"


def decode_dashboard_payload(payload: bytes) -> dict[str, Any]:
    """Decode JSON, or only the JSON header of an IMC1 sensor packet."""
    if not payload.startswith(SENSOR_PACKET_WIRE_MAGIC):
        value = json.loads(payload.decode("utf-8"))
    else:
        if len(payload) < 8:
            raise ValueError("binary sensor packet header is truncated")
        header_length = struct.unpack("<I", payload[4:8])[0]
        header_end = 8 + header_length
        if header_end > len(payload):
            raise ValueError("binary sensor packet header length is invalid")
        value = json.loads(payload[8:header_end].decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("MQTT payload must contain a JSON object")
    return value
