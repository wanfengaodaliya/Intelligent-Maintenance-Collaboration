from __future__ import annotations

import pytest

from cloud_service.service import _validate_v12_request
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from diagnosis_window import DiagnosisWindowAssembler


def _packet(sequence: int) -> dict:
    return {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "packet_id": f"packet_{sequence:03d}",
        "sequence_number": sequence, "start_generate_timestamp_ns": (sequence - 1) * 50_000_000,
        "end_generate_timestamp_ns": sequence * 50_000_000,
        "data": {"vibration": {"sample_rate_hz": 64_000, "sample_count": 2, "values": [1.0, 2.0]}},
    }


@pytest.mark.parametrize("window_ms", [50, 100, 150])
def test_edge_assembled_manifest_is_accepted_unchanged_by_cloud(window_ms: int) -> None:
    assembler = DiagnosisWindowAssembler(
        window_ms=window_ms, step_ms=window_ms, overlap_enabled=False,
    )
    window = next(
        result for sequence in range(1, window_ms // 50 + 1)
        for result in assembler.append(_packet(sequence))
    )
    raw_window = {
        "device_id": window.device_id, "task_id": window.task_id,
        "bearing_id": window.bearing_id, "sender_id": window.sender_id,
        "window_start_sequence": window.window_start_sequence,
        "window_end_sequence": window.window_end_sequence,
        "window_start_ns": window.window_start_ns, "window_end_ns": window.window_end_ns,
        "contributing_packet_ids": list(window.contributing_packet_ids),
        "sample_rate_hz": 64_000, "sample_count": 2 * len(window.packets),
        "data": {"vibration": {"sample_rate_hz": 64_000, "values": [1.0, 2.0]}},
    }
    request = {
        "schema_version": "cloud-infer/2.0",
        "decision_round_id": window.decision_round_id,
        "diagnosis_window_id": window.diagnosis_window_id,
        "edge_perception_result": {}, "cloud_raw_window": raw_window,
    }

    accepted = _validate_v12_request(request)

    assert accepted["contributing_packet_ids"] == list(window.contributing_packet_ids)
    assert accepted["window_start_sequence"] == window.window_start_sequence
    assert accepted["window_end_sequence"] == window.window_end_sequence
