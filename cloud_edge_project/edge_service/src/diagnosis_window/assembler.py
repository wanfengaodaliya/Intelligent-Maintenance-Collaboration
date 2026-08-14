"""Non-overlapping 50/100/150ms diagnosis-window assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.diagnosis_identity import DiagnosisIdentity


class DiagnosisWindowError(ValueError):
    pass


@dataclass(frozen=True)
class DiagnosisWindow:
    diagnosis_window_id: str
    decision_round_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    window_start_sequence: int
    window_end_sequence: int
    window_start_ns: int
    window_end_ns: int
    contributing_packet_ids: tuple[str, ...]
    packets: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class IncompleteTailReport:
    task_id: str
    device_id: str
    bearing_id: str
    sender_id: str
    incomplete_tail_packet_count: int
    incomplete_tail_packet_ids: tuple[str, ...] = ()
    incomplete_tail_sequences: tuple[int, ...] = ()


class DiagnosisWindowAssembler:
    def __init__(
        self, *, window_ms: int, packet_duration_ms: int = 50,
        step_ms: int | None = None, overlap_enabled: bool = False,
    ) -> None:
        if window_ms not in {50, 100, 150}:
            raise ValueError("window_ms must be one of 50, 100, or 150")
        if packet_duration_ms != 50 or window_ms % packet_duration_ms:
            raise ValueError("packet_duration_ms must be 50 and divide window_ms")
        if step_ms is not None and step_ms != window_ms:
            raise ValueError("step_ms must equal window_ms")
        if overlap_enabled:
            raise ValueError("overlap_enabled must be false")
        self._packets_per_window = window_ms // packet_duration_ms
        self._pending: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        self._next_sequence: dict[tuple[str, str, str, str], int] = {}

    def append(self, packet: Mapping[str, Any]) -> list[DiagnosisWindow]:
        normalized = _normalize_packet(packet)
        key = _key(normalized)
        pending = self._pending.setdefault(key, [])
        expected_sequence = self._next_sequence.get(key, 1)
        if normalized["sequence_number"] != expected_sequence:
            raise DiagnosisWindowError("packet sequence is not contiguous")
        if pending:
            _validate_compatible(pending[0], normalized)
        pending.append(normalized)
        self._next_sequence[key] = expected_sequence + 1
        if len(pending) < self._packets_per_window:
            return []

        packets = tuple(pending)
        self._pending[key] = []
        identity = DiagnosisIdentity(
            device_id=packets[0]["device_id"],
            task_id=packets[0]["task_id"],
            bearing_id=packets[0]["bearing_id"],
            sender_id=packets[0]["sender_id"],
            window_start_sequence=packets[0]["sequence_number"],
            window_end_sequence=packets[-1]["sequence_number"],
        )
        return [
            DiagnosisWindow(
                diagnosis_window_id=identity.diagnosis_window_id,
                decision_round_id=identity.decision_round_id,
                device_id=identity.device_id,
                task_id=identity.task_id,
                bearing_id=identity.bearing_id,
                sender_id=identity.sender_id,
                window_start_sequence=identity.window_start_sequence,
                window_end_sequence=identity.window_end_sequence,
                window_start_ns=packets[0]["start_generate_timestamp_ns"],
                window_end_ns=packets[-1]["end_generate_timestamp_ns"],
                contributing_packet_ids=tuple(item["packet_id"] for item in packets),
                packets=packets,
            )
        ]

    def finish_task(self, task_id: str) -> IncompleteTailReport:
        matches = [(key, pending) for key, pending in self._pending.items() if key[1] == task_id and pending]
        if len(matches) != 1:
            raise DiagnosisWindowError("task must have exactly one incomplete window")
        key, _pending = matches[0]
        return self.finish_subject(
            device_id=key[0], task_id=key[1], bearing_id=key[2], sender_id=key[3]
        )

    def finish_subject(
        self, *, device_id: str, task_id: str, bearing_id: str, sender_id: str
    ) -> IncompleteTailReport:
        key = (device_id, task_id, bearing_id, sender_id)
        pending = self._pending.get(key, [])
        if not pending:
            raise DiagnosisWindowError("subject has no incomplete window")
        self._pending[key] = []
        return IncompleteTailReport(
            device_id=key[0], task_id=key[1], bearing_id=key[2], sender_id=key[3],
            incomplete_tail_packet_count=len(pending),
            incomplete_tail_packet_ids=tuple(item["packet_id"] for item in pending),
            incomplete_tail_sequences=tuple(item["sequence_number"] for item in pending),
        )


def _key(packet: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(packet[field] for field in ("device_id", "task_id", "bearing_id", "sender_id"))


def _normalize_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "device_id", "task_id", "bearing_id", "sender_id", "packet_id", "sequence_number",
        "start_generate_timestamp_ns", "end_generate_timestamp_ns", "data",
    }
    if not isinstance(packet, Mapping) or not required.issubset(packet):
        raise DiagnosisWindowError("packet does not contain the diagnosis-window identity")
    result = dict(packet)
    if any(not isinstance(result[field], str) or not result[field] for field in required - {"sequence_number", "start_generate_timestamp_ns", "end_generate_timestamp_ns", "data"}):
        raise DiagnosisWindowError("packet identity is invalid")
    if (
        isinstance(result["sequence_number"], bool)
        or not isinstance(result["sequence_number"], int)
        or result["sequence_number"] < 1
        or any(
            isinstance(result[field], bool)
            or not isinstance(result[field], int)
            or result[field] < 0
            for field in ("start_generate_timestamp_ns", "end_generate_timestamp_ns")
        )
    ):
        raise DiagnosisWindowError("packet sequence or timestamp is invalid")
    if result["end_generate_timestamp_ns"] <= result["start_generate_timestamp_ns"]:
        raise DiagnosisWindowError("packet time range is invalid")
    if not isinstance(result["data"], Mapping) or not result["data"]:
        raise DiagnosisWindowError("packet data is invalid")
    return result


def _validate_compatible(first: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    if current["start_generate_timestamp_ns"] != first["end_generate_timestamp_ns"] + (current["sequence_number"] - first["sequence_number"] - 1) * (first["end_generate_timestamp_ns"] - first["start_generate_timestamp_ns"]):
        raise DiagnosisWindowError("packet timestamps are not contiguous")
    if set(current["data"]) != set(first["data"]):
        raise DiagnosisWindowError("packet channel sets do not match")
    for channel, source in first["data"].items():
        next_source = current["data"][channel]
        if isinstance(source, Mapping) != isinstance(next_source, Mapping):
            raise DiagnosisWindowError("packet channel shape does not match")
        if isinstance(source, Mapping) and source.get("sample_rate_hz") != next_source.get("sample_rate_hz"):
            raise DiagnosisWindowError("packet sample rate does not match")
