"""Freeze contiguous high-rate packet history without padding or cross-task joins."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .contracts import CaptureDecision, RawAnalysisSample


class RawSampleFreezer:
    def freeze(
        self, decision: CaptureDecision, packet_buffer: Sequence[Mapping[str, Any]]
    ) -> RawAnalysisSample:
        packets = _contiguous_packets(decision, packet_buffer)
        if not packets:
            raise ValueError("raw packet buffer has no matching packet")
        rate = _vibration_rate(packets[-1])
        selected: list[Mapping[str, Any]] = []
        count = 0
        required_count = decision.requested_window_ms * rate // 1_000
        for packet in reversed(packets):
            if _vibration_rate(packet) != rate:
                break
            selected.append(packet)
            count += _vibration_count(packet)
            if count >= required_count:
                break
        selected.reverse()
        actual_window_ms = count * 1_000 // rate
        actual_window_ms = min(actual_window_ms, decision.requested_window_ms)
        selected_payload = [_json_packet(packet) for packet in selected]
        payload = json.dumps(
            {"packets": selected_payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        manifest = tuple(_manifest_item(packet) for packet in selected)
        sample_id = "ras_" + hashlib.sha256(
            json.dumps(
                {
                    "device_id": decision.device_id,
                    "task_id": decision.task_id,
                    "bearing_id": decision.bearing_id,
                    "sender_id": decision.sender_id,
                    "decision_round_id": decision.decision_round_id,
                    "trigger_reasons": decision.trigger_reasons,
                    "requested_window_ms": decision.requested_window_ms,
                    "packet_manifest": manifest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        end_ns = int(selected[-1]["end_generate_timestamp_ns"])
        return RawAnalysisSample(
            sample_id=sample_id,
            device_id=decision.device_id,
            task_id=decision.task_id,
            bearing_id=decision.bearing_id,
            sender_id=decision.sender_id,
            decision_round_id=decision.decision_round_id,
            trigger_reasons=decision.trigger_reasons,
            sample_type=decision.sample_type,
            requested_window_ms=decision.requested_window_ms,
            actual_window_ms=actual_window_ms,
            complete=actual_window_ms == decision.requested_window_ms,
            missing_duration_ms=decision.requested_window_ms - actual_window_ms,
            window_start_ns=end_ns - actual_window_ms * 1_000_000,
            window_end_ns=end_ns,
            packet_manifest=manifest,
            sample_rate_hz=rate,
            sample_count=count,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            edge_model_version=decision.edge_model_version,
            cloud_corrected=decision.cloud_corrected,
            created_at_ns=decision.created_at_ns,
            payload=payload,
        )


def _contiguous_packets(
    decision: CaptureDecision, packet_buffer: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    matching = [
        packet for packet in packet_buffer
        if all(packet.get(field) == getattr(decision, field) for field in (
            "device_id", "task_id", "bearing_id", "sender_id"
        ))
        and isinstance(packet.get("sequence_number"), int)
        and isinstance(packet.get("end_generate_timestamp_ns"), int)
        and packet["end_generate_timestamp_ns"] <= decision.created_at_ns
    ]
    ordered = sorted(matching, key=lambda item: (item["sequence_number"], item["end_generate_timestamp_ns"]))
    if not ordered:
        return []
    selected = [ordered[-1]]
    for packet in reversed(ordered[:-1]):
        if packet["sequence_number"] != selected[0]["sequence_number"] - 1:
            break
        selected.insert(0, packet)
    return selected


def _vibration(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        source = packet["data"]["vibration"]
    except (KeyError, TypeError) as exc:
        raise ValueError("packet has no vibration waveform") from exc
    if not isinstance(source, Mapping):
        raise ValueError("packet vibration waveform is invalid")
    return source


def _vibration_rate(packet: Mapping[str, Any]) -> int:
    rate = _vibration(packet).get("sample_rate_hz")
    if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
        raise ValueError("packet vibration sample_rate_hz is invalid")
    return rate


def _vibration_count(packet: Mapping[str, Any]) -> int:
    source = _vibration(packet)
    count = source.get("sample_count")
    values = source.get("values")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("packet vibration sample_count is invalid")
    if not hasattr(values, "__len__") or len(values) != count:
        raise ValueError("packet vibration values do not match sample_count")
    return count


def _manifest_item(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "packet_id": packet["packet_id"],
        "sequence_number": packet["sequence_number"],
        "end_generate_timestamp_ns": packet["end_generate_timestamp_ns"],
        "sample_rate_hz": _vibration_rate(packet),
        "sample_count": _vibration_count(packet),
    }


def _json_packet(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_packet(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_packet(item) for item in value]
    if isinstance(value, list):
        return [_json_packet(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_packet(value.tolist())
    return value
