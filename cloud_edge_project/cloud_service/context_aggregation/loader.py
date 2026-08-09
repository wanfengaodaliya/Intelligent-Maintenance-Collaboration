from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from cloud_service.storage.database import connect
from cloud_service.storage.raw_packet_repository import (
    PayloadHashMismatch,
    RawPacketRepository,
)

from .contracts import AggregationError, HIGH_RATE_CHANNELS, LoadedPacket


_HIGH_RATE = {"vibration": "mm/s", "phase_current_1_A": "A", "phase_current_2_A": "A"}
_CONTEXT = ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n")


class ContextWindowLoader:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.packets = RawPacketRepository(self.database_path)

    def load(self, review_id: str) -> tuple[dict, dict, list[LoadedPacket]]:
        with connect(self.database_path) as connection:
            review = connection.execute("SELECT * FROM cloud_review WHERE review_id=?", (review_id,)).fetchone()
            request = connection.execute("SELECT * FROM raw_context_request WHERE review_id=?", (review_id,)).fetchone()
            links = connection.execute(
                "SELECT * FROM review_context_packets WHERE review_id=? AND role='before' ORDER BY relative_position",
                (review_id,),
            ).fetchall()
        if review is None or request is None:
            raise AggregationError("AGGREGATION_NOT_ELIGIBLE", "review or context request does not exist")
        review, request = dict(review), dict(request)
        if request["request_status"] != review["context_status"] or request["request_status"] not in {"complete", "partial_context"}:
            raise AggregationError("AGGREGATION_NOT_ELIGIBLE", "context request is not eligible")
        if any(
            review[field] != request[field]
            for field in ("device_id", "bearing_id", "sender_id", "anchor_packet_id")
        ):
            raise AggregationError("ANCHOR_MISMATCH", "review and request anchors differ")
        anchor = self._load_one(request, 0, request["anchor_packet_id"], None)
        anchor_time = anchor.packet["end_generate_timestamp_ns"]
        loaded = [anchor] + [
            self._load_one(request, row["relative_position"], row["packet_id"], anchor_time)
            for row in links
        ]
        loaded.sort(key=lambda item: item.relative_position)
        positions = [item.relative_position for item in loaded]
        expected = list(range(-20, 1)) if request["request_status"] == "complete" else list(range(min(positions), 1))
        if positions != expected or (request["request_status"] == "partial_context" and not -19 <= min(positions) <= -16):
            raise AggregationError("CONTEXT_SEQUENCE_GAP", "context packets are not a continuous eligible suffix")
        return review, request, loaded

    def _load_one(self, request: dict, position: int, packet_id: str, anchor_time: int | None) -> LoadedPacket:
        try:
            packet, index = self.packets.load_indexed_packet(request["sender_id"], packet_id)
        except KeyError as error:
            raise AggregationError("RAW_PACKET_UNAVAILABLE", str(error)) from error
        except PayloadHashMismatch as error:
            raise AggregationError("PAYLOAD_HASH_MISMATCH", str(error)) from error
        except (OSError, ValueError) as error:
            raise AggregationError("RAW_PACKET_UNAVAILABLE", str(error)) from error
        if index["validation_status"] not in {"valid", "warning"}:
            raise AggregationError("RAW_PACKET_UNAVAILABLE", "indexed raw packet is invalid")
        for field in ("device_id", "bearing_id", "sender_id"):
            if packet.get(field) != request[field] or index.get(field) != request[field]:
                raise AggregationError("PACKET_IDENTITY_MISMATCH", f"packet {field} does not match request")
        sequence = packet.get("sequence_number")
        if not isinstance(sequence, int) or sequence - request["anchor_sequence_number"] != position:
            raise AggregationError("RELATIVE_POSITION_INVALID", "packet sequence does not match position")
        if position == 0 and packet_id != request["anchor_packet_id"]:
            raise AggregationError("ANCHOR_MISMATCH", "anchor packet does not match request")
        expected_time = index["end_generate_timestamp_ns"]
        if packet.get("end_generate_timestamp_ns") != expected_time or (anchor_time is not None and expected_time != anchor_time + position * 50_000_000):
            raise AggregationError("TIMESTAMP_DISCONTINUITY", "packet timestamp is discontinuous")
        self._validate_signals(packet)
        return LoadedPacket(packet=packet, index=index, relative_position=position)

    @staticmethod
    def _validate_signals(packet: dict) -> None:
        data = packet.get("data")
        if not isinstance(data, dict):
            raise AggregationError("HIGH_RATE_SIGNAL_INVALID", "packet data is missing")
        for name, unit in _HIGH_RATE.items():
            ContextWindowLoader._validate_signal(data.get(name), name, 64_000, 3_200, unit, "HIGH_RATE_SIGNAL_INVALID")
        for name in _CONTEXT:
            values = ContextWindowLoader._validate_signal(data.get(name), name, 4_000, 200, None, "OPERATING_CONTEXT_INVALID")
            if name in {"shaft_speed_rpm", "bearing_radial_load_n"} and any(value < 0 for value in values):
                raise AggregationError("OPERATING_CONTEXT_INVALID", f"{name} must be non-negative")

    @staticmethod
    def _validate_signal(signal: object, name: str, rate: int, count: int, unit: str | None, code: str) -> list[float]:
        if not isinstance(signal, dict) or signal.get("sample_rate_hz") != rate or signal.get("sample_count") != count or (unit is not None and signal.get("unit") != unit):
            raise AggregationError(code, f"{name} configuration is invalid")
        values = signal.get("values")
        if not isinstance(values, list) or len(values) != count or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise AggregationError("SIGNAL_VALUE_OR_UNIT_INVALID", f"{name} values are invalid")
        return values


def source_fingerprint(review_id: str, context_status: str, packets: list[LoadedPacket]) -> str:
    manifest = [{"device_id": item.packet["device_id"], "task_id": item.packet["task_id"], "bearing_id": item.packet["bearing_id"], "sender_id": item.packet["sender_id"], "relative_position": item.relative_position, "packet_id": item.packet["packet_id"], "payload_sha256": item.index["payload_sha256"]} for item in packets]
    payload = json.dumps({"review_id": review_id, "context_status": context_status, "packets": manifest}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
