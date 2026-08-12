from __future__ import annotations

from pathlib import Path

import pytest

from cloud_service.bearing_review.service import (
    BearingReviewConflictError,
    BearingReviewService,
    BearingReviewValidationError,
)
from cloud_service.bearing_review.receiver import BearingRawContextReceiver


class CapturingTransport:
    def __init__(self):
        self.requests: list[dict] = []

    def send(self, request: dict) -> dict:
        self.requests.append(request)
        return {"status": "accepted"}


def _request() -> dict:
    return {
        "scenario_type": "bearing",
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "edge_bearing_result": {
            "bearing_state": "warning",
            "confidence": 0.62,
            "packet_count": 20,
        },
        "source_packet_manifest": [
            {"packet_id": f"packet_{number:02d}", "sequence_number": number}
            for number in range(1, 21)
        ],
    }


def test_bearing_review_requests_exact_manifest_and_is_idempotent(tmp_path: Path):
    transport = CapturingTransport()
    service = BearingReviewService(tmp_path / "cloud.db", transport=transport)

    first = service.create(_request())
    repeated = service.create(_request())

    assert first["status"] == "WAITING_FOR_CONTEXT"
    assert repeated == first
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["review_type"] == "bearing_review"
    assert request["expected_packet_count"] == 20
    assert request["requested_packets"] == _request()["source_packet_manifest"]


def test_bearing_review_forwards_optional_edge_node_id(tmp_path: Path):
    transport = CapturingTransport()
    payload = _request()
    payload["edge_node_id"] = "edge_02"

    BearingReviewService(tmp_path / "cloud.db", transport=transport).create(payload)

    assert transport.requests[0]["edge_node_id"] == "edge_02"


def test_bearing_review_rejects_manifest_that_is_not_exactly_twenty_packets(tmp_path: Path):
    payload = _request()
    payload["source_packet_manifest"] = payload["source_packet_manifest"][:-1]

    with pytest.raises(BearingReviewValidationError, match="INVALID_SOURCE_PACKET_MANIFEST"):
        BearingReviewService(tmp_path / "cloud.db", transport=CapturingTransport()).create(payload)


def test_bearing_review_rejects_different_manifest_for_same_identity(tmp_path: Path):
    transport = CapturingTransport()
    service = BearingReviewService(tmp_path / "cloud.db", transport=transport)
    service.create(_request())
    changed = _request()
    changed["source_packet_manifest"][-1]["packet_id"] = "packet_replaced"

    with pytest.raises(BearingReviewConflictError, match="BEARING_REVIEW_MANIFEST_CONFLICT"):
        service.create(changed)


def test_bearing_review_allows_four_windows_for_same_task_bearing(tmp_path: Path):
    service = BearingReviewService(
        tmp_path / "cloud.db", transport=CapturingTransport()
    )
    first = service.create(_request())
    second_request = _request()
    second_request["source_packet_manifest"] = [
        {"packet_id": f"packet_{number:02d}", "sequence_number": number}
        for number in range(21, 41)
    ]
    second = service.create(second_request)

    assert first["window_index"] == 1
    assert second["window_index"] == 2
    assert first["bearing_review_id"] != second["bearing_review_id"]


def test_bearing_review_rejects_context_packet_outside_manifest(tmp_path: Path):
    transport = CapturingTransport()
    database_path = tmp_path / "cloud.db"
    created = BearingReviewService(database_path, transport=transport).create(_request())

    with pytest.raises(BearingReviewValidationError, match="CONTEXT_PACKET_NOT_REQUESTED") as error:
        BearingRawContextReceiver(database_path).receive_batch(
            {
                "request_id": created["raw_context_request_id"],
                "review_type": "bearing_review",
                "device_id": "device_01",
                "task_id": "task_01",
                "bearing_id": "bearing_01",
                "sender_id": "sender_01",
                "packets": [{"packet_id": "unrequested", "sequence_number": 1}],
            }
        )
    assert error.value.code == "CONTEXT_PACKET_NOT_REQUESTED"


def _raw_packet(number: int) -> dict:
    def signal(sample_rate_hz: int, sample_count: int, value: float) -> dict:
        return {"sample_rate_hz": sample_rate_hz, "sample_count": sample_count, "values": [value] * sample_count}

    return {
        "device_id": "device_01", "task_id": "task_01", "bearing_id": "bearing_01", "sender_id": "sender_01",
        "packet_id": f"packet_{number:02d}", "sequence_number": number,
        "data": {
            "vibration": signal(64_000, 3_200, 0.1),
            "phase_current_1_A": signal(64_000, 3_200, 0.1),
            "phase_current_2_A": signal(64_000, 3_200, 0.1),
            "shaft_speed_rpm": signal(4_000, 200, 1.0),
            "load_torque_nm": signal(4_000, 200, 1.0),
            "bearing_radial_load_n": signal(4_000, 200, 1.0),
            "bearing_module_temperature_c": 25.0,
        },
    }


def test_bearing_review_produces_structured_result_only_after_all_twenty_packets(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    created = BearingReviewService(database_path, transport=CapturingTransport()).create(_request())
    receiver = BearingRawContextReceiver(database_path)
    base = {
        "request_id": created["raw_context_request_id"], "review_type": "bearing_review",
        "device_id": "device_01", "task_id": "task_01", "bearing_id": "bearing_01", "sender_id": "sender_01",
    }

    receiver.receive_batch({**base, "packets": [_raw_packet(number) for number in range(1, 20)]})
    incomplete = BearingReviewService(database_path, transport=CapturingTransport()).get(created["bearing_review_id"])
    assert incomplete["status"] == "WAITING_FOR_CONTEXT"
    assert incomplete["received_packet_count"] == 19

    receiver.receive_batch({**base, "packets": [_raw_packet(20)]})
    completed = BearingReviewService(database_path, transport=CapturingTransport()).get(created["bearing_review_id"])
    assert completed["status"] == "SUCCEEDED"
    assert completed["received_packet_count"] == 20
    assert completed["cloud_bearing_result"]["review_packet_count"] == 20
    assert completed["cloud_bearing_result"]["result_source"] == "cloud_bearing_review"
