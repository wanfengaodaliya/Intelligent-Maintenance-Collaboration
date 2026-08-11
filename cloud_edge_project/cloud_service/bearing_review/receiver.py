"""Receive only the exact high-rate packets named by a bearing manifest."""

from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
from typing import Any

from .contracts import BearingReviewConflictError, BearingReviewValidationError
from .repository import BearingReviewRepository
from .processing import BearingReviewProcessor


class BearingRawContextReceiver:
    def __init__(self, database_path: Path):
        self.repository = BearingReviewRepository(database_path)

    def receive_batch(self, payload: Any) -> dict[str, Any]:
        batch = _validate_batch(payload)
        request = self.repository.context_request(batch["request_id"])
        if request is None:
            raise BearingReviewValidationError("UNKNOWN_CONTEXT_REQUEST")
        if request["status"] != "WAITING_FOR_CONTEXT":
            raise BearingReviewValidationError("CONTEXT_REQUEST_NOT_WAITING")
        for field in ("device_id", "task_id", "bearing_id", "sender_id"):
            if batch[field] != request[field]:
                raise BearingReviewValidationError("CONTEXT_REQUEST_MISMATCH")
        requested = {
            item["packet_id"]: item["sequence_number"]
            for item in json.loads(request["requested_packets_json"])
        }
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_sequences: set[int] = set()
        for packet in batch["packets"]:
            packet_id = packet.get("packet_id") if isinstance(packet, dict) else None
            sequence = packet.get("sequence_number") if isinstance(packet, dict) else None
            if packet_id not in requested or requested.get(packet_id) != sequence:
                raise BearingReviewValidationError("CONTEXT_PACKET_NOT_REQUESTED")
            if packet_id in seen_ids or sequence in seen_sequences:
                raise BearingReviewValidationError("INVALID_CONTEXT_PACKET")
            seen_ids.add(packet_id)
            seen_sequences.add(sequence)
            _validate_raw_packet(packet, request)
            status = self.repository.add_context_packet(request["bearing_review_id"], packet)
            results.append({"packet_id": packet_id, "sequence_number": sequence, "status": status})
        received = self.repository.mark_processing_if_complete(request["bearing_review_id"])
        if received == 20:
            BearingReviewProcessor(self.repository).process(request["bearing_review_id"])
        return {
            "request_id": request["raw_context_request_id"],
            "status": "accepted",
            "received_packet_count": received,
            "expected_packet_count": 20,
            "results": results,
        }


def _validate_batch(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("review_type") != "bearing_review":
        raise BearingReviewValidationError("INVALID_CONTEXT_BATCH")
    for field in ("request_id", "device_id", "task_id", "bearing_id", "sender_id"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise BearingReviewValidationError("INVALID_CONTEXT_BATCH")
    packets = payload.get("packets")
    if not isinstance(packets, list) or not 1 <= len(packets) <= 20:
        raise BearingReviewValidationError("INVALID_CONTEXT_BATCH")
    return payload


def _validate_raw_packet(packet: Any, request: dict[str, Any]) -> None:
    if not isinstance(packet, dict):
        raise BearingReviewValidationError("INVALID_CONTEXT_PACKET")
    for field in ("device_id", "task_id", "bearing_id", "sender_id", "packet_id"):
        if field != "packet_id" and packet.get(field) != request[field]:
            raise BearingReviewValidationError("CONTEXT_REQUEST_MISMATCH")
    if not isinstance(packet.get("sequence_number"), int) or isinstance(packet["sequence_number"], bool) or packet["sequence_number"] <= 0:
        raise BearingReviewValidationError("INVALID_CONTEXT_PACKET")
    data = packet.get("data")
    if not isinstance(data, dict):
        raise BearingReviewValidationError("INVALID_CONTEXT_PACKET")
    for name, rate, count in (
        ("vibration", 64_000, 3_200),
        ("phase_current_1_A", 64_000, 3_200),
        ("phase_current_2_A", 64_000, 3_200),
        ("shaft_speed_rpm", 4_000, 200),
        ("load_torque_nm", 4_000, 200),
        ("bearing_radial_load_n", 4_000, 200),
    ):
        signal = data.get(name)
        if not isinstance(signal, dict) or signal.get("sample_rate_hz") != rate or signal.get("sample_count") != count:
            raise BearingReviewValidationError("INVALID_SAMPLE_CONFIG")
        values = signal.get("values")
        if not isinstance(values, list) or len(values) != count or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(float(value)) for value in values):
            raise BearingReviewValidationError("INVALID_SIGNAL_SHAPE")
    temperature = data.get("bearing_module_temperature_c")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not isfinite(float(temperature)):
        raise BearingReviewValidationError("INVALID_CONTEXT_PACKET")
