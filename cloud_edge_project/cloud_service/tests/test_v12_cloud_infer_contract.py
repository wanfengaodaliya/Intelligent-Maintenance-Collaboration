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
