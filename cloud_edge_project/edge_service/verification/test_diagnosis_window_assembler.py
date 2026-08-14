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


@pytest.mark.parametrize(
    ("window_ms", "expected_ranges"),
    [
        (50, [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6)]),
        (100, [(1, 2), (3, 4), (5, 6)]),
        (150, [(1, 3), (4, 6)]),
    ],
)
def test_assembler_emits_only_non_overlapping_windows(window_ms, expected_ranges) -> None:
    assembler = DiagnosisWindowAssembler(window_ms=window_ms)
    windows = [window for sequence in range(1, 7) for window in assembler.append(_packet(sequence))]

    assert [(item.window_start_sequence, item.window_end_sequence) for item in windows] == expected_ranges
    assert [packet_id for item in windows for packet_id in item.contributing_packet_ids] == [
        f"packet_{sequence:03d}" for sequence in range(1, 7)
    ]


def test_assembler_rejects_out_of_order_or_incompatible_packets() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=100)
    with pytest.raises(DiagnosisWindowError, match="sequence"):
        assembler.append(_packet(2))

    assembler.append(_packet(1))
    with pytest.raises(DiagnosisWindowError, match="sample rate"):
        assembler.append(_packet(2, sample_rate=16_000))


def test_finish_task_reports_tail_without_creating_a_partial_window() -> None:
    assembler = DiagnosisWindowAssembler(window_ms=150)
    assert assembler.append(_packet(1)) == []
    assert assembler.append(_packet(2)) == []

    report = assembler.finish_task("task_001")
    assert report.incomplete_tail_packet_count == 2
    assert report.task_id == "task_001"


def test_assembler_rejects_overlap_or_step_that_differs_from_window() -> None:
    with pytest.raises(ValueError, match="step_ms"):
        DiagnosisWindowAssembler(window_ms=100, step_ms=50)
    with pytest.raises(ValueError, match="overlap_enabled"):
        DiagnosisWindowAssembler(window_ms=100, overlap_enabled=True)
