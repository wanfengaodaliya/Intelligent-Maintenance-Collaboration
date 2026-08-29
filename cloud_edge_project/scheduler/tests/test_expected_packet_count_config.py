from __future__ import annotations

import pytest

from scheduler.assignment_scheduler import AssignmentError, validate_assignment_request


def _request(expected_packet_count: int) -> dict:
    return {
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_size_bytes": 1024,
        "expected_packet_count": expected_packet_count,
        "expected_duration_ms": expected_packet_count * 50,
        "created_timestamp_ns": 1,
    }


def test_scheduler_accepts_explicit_smoke_packet_count(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_EXPECTED_PACKET_COUNT", "15")
    assert validate_assignment_request(_request(15))["expected_packet_count"] == 15

    with pytest.raises(AssignmentError, match="must equal 15"):
        validate_assignment_request(_request(80))


def test_scheduler_accepts_optional_run_id(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_EXPECTED_PACKET_COUNT", "80")
    request = _request(80)
    request["run_id"] = "run_batch01"

    assert validate_assignment_request(request)["run_id"] == "run_batch01"


def test_scheduler_still_rejects_unknown_fields(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_EXPECTED_PACKET_COUNT", "80")
    request = _request(80)
    request["unknown_field"] = "unexpected"

    with pytest.raises(AssignmentError, match="unknown_field"):
        validate_assignment_request(request)
