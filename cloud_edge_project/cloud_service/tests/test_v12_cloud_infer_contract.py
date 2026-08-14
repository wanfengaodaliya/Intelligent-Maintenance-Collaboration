from __future__ import annotations

from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from cloud_service.errors import CloudServiceError
from cloud_service.service import _validate_v12_request


def _request() -> dict:
    window = {
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_02",
        "sender_id": "sender_02", "window_start_sequence": 1, "window_end_sequence": 1,
        "window_start_ns": 0, "window_end_ns": 50_000_000,
        "contributing_packet_ids": ["packet_001"], "sample_rate_hz": 64_000,
        "sample_count": 3_200, "data": {},
    }
    return {
        "schema_version": "cloud-infer/2.0",
        "decision_round_id": build_decision_round_id(
            device_id="machine_01", task_id="task_001", window_start_sequence=1, window_end_sequence=1,
        ),
        "diagnosis_window_id": build_diagnosis_window_id(
            device_id="machine_01", task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
            window_start_sequence=1, window_end_sequence=1,
        ),
        "edge_perception_result": {},
        "cloud_raw_window": window,
    }


def test_cloud_infer_v12_accepts_consistent_window_manifest() -> None:
    request = _request()

    assert _validate_v12_request(request) == request["cloud_raw_window"]


def test_cloud_infer_v12_rejects_mismatched_diagnosis_window_identity() -> None:
    request = _request()
    request["diagnosis_window_id"] = "dw_other"

    try:
        _validate_v12_request(request)
    except CloudServiceError as error:
        assert error.code == "INVALID_CLOUD_WINDOW"
    else:
        raise AssertionError("cloud infer must reject a mismatched window identity")


def test_cloud_infer_v12_keeps_the_same_non_overlapping_window_manifest() -> None:
    request = _request()
    request["cloud_raw_window"].update({
        "window_end_sequence": 3,
        "window_end_ns": 150_000_000,
        "contributing_packet_ids": ["packet_001", "packet_002", "packet_003"],
        "sample_count": 9_600,
    })
    request["decision_round_id"] = build_decision_round_id(
        device_id="machine_01", task_id="task_001", window_start_sequence=1, window_end_sequence=3,
    )
    request["diagnosis_window_id"] = build_diagnosis_window_id(
        device_id="machine_01", task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
        window_start_sequence=1, window_end_sequence=3,
    )

    assert _validate_v12_request(request)["contributing_packet_ids"] == [
        "packet_001", "packet_002", "packet_003",
    ]


def test_cloud_infer_v12_rejects_manifest_that_does_not_cover_its_sequence_range() -> None:
    request = _request()
    request["cloud_raw_window"]["window_end_sequence"] = 2
    request["decision_round_id"] = build_decision_round_id(
        device_id="machine_01", task_id="task_001", window_start_sequence=1, window_end_sequence=2,
    )
    request["diagnosis_window_id"] = build_diagnosis_window_id(
        device_id="machine_01", task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
        window_start_sequence=1, window_end_sequence=2,
    )

    try:
        _validate_v12_request(request)
    except CloudServiceError as error:
        assert error.code == "INVALID_CLOUD_WINDOW"
    else:
        raise AssertionError("cloud infer must reject a shortened window manifest")
