from __future__ import annotations

import pytest

from diagnosis_window import DiagnosisWindowAssembler, DiagnosisWindowError


def _packet(sequence: int, *, task_id: str = "task_001", sample_rate: int = 64_000) -> dict:
    start = (sequence - 1) * 50_000_000
    return {
        "device_id": "machine_01",
        "task_id": task_id,
        "bearing_id": "bearing_02",
        "sender_id": "sender_02",
        "packet_id": f"packet_{sequence:03d}",
        "sequence_number": sequence,
        "start_generate_timestamp_ns": start,
        "end_generate_timestamp_ns": start + 50_000_000,
        "data": {
            "vibration": {
                "sample_rate_hz": sample_rate,
                "sample_count": 2,
                "values": [float(sequence), float(sequence) + 0.5],
            }
        },
    }


def test_assembler_emits_one_window_per_50ms_packet() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=50)
    windows = [window for sequence in range(1, 7) for window in assembler.append(_packet(sequence))]

    assert [(item.window_start_sequence, item.window_end_sequence) for item in windows] == [
        (sequence, sequence) for sequence in range(1, 7)
    ]
    assert [item.contributing_packet_ids for item in windows] == [
        (f"packet_{sequence:03d}",) for sequence in range(1, 7)
    ]


@pytest.mark.parametrize("window_ms", [100, 150])
def test_assembler_rejects_non_50ms_windows(window_ms) -> None:
    with pytest.raises(ValueError, match="locked at 50"):
        DiagnosisWindowAssembler(window_ms=window_ms)


def test_assembler_rejects_out_of_order_packets() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=50)
    with pytest.raises(DiagnosisWindowError, match="sequence"):
        assembler.append(_packet(2))


def test_finish_task_without_pending_window_reports_error() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=50)
    with pytest.raises(DiagnosisWindowError, match="exactly one incomplete window"):
        assembler.finish_task("task_001")


def test_assembler_rejects_overlap_or_step_that_differs_from_window() -> None:
    with pytest.raises(ValueError, match="step_ms"):
        DiagnosisWindowAssembler(window_ms=50, step_ms=25)
    with pytest.raises(ValueError, match="overlap_enabled"):
        DiagnosisWindowAssembler(window_ms=50, overlap_enabled=True)