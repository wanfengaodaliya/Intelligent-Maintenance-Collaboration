from __future__ import annotations

import pytest

from core.diagnosis_identity import build_run_id
from scheduler.assignment_scheduler import AssignmentError, validate_assignment_request
from scheduler.task_repository import TaskRepository


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


def test_scheduler_derives_and_validates_shared_run_id() -> None:
    request = _request(80)
    expected = build_run_id(
        device_id="machine_01", batch_created_timestamp_ns=1
    )
    assert validate_assignment_request(request)["run_id"] == expected
    assert validate_assignment_request(request | {"run_id": expected})["run_id"] == expected

    with pytest.raises(AssignmentError, match="run_id must match"):
        validate_assignment_request(request | {"run_id": "run_wrong"})


def test_repository_persists_run_id_for_peer_edge_exclusion(tmp_path) -> None:
    repository = TaskRepository(tmp_path / "scheduler.db")
    request = validate_assignment_request(_request(80))
    claim_id = "claim_01"
    claimed = repository.claim(request, claim_id)
    attempt_id, _ = repository.start_attempt(
        request["task_id"], "edge_01", claim_id, bearing_id=request["bearing_id"]
    )
    repository.accept_attempt(
        attempt_id,
        request["task_id"],
        claim_id,
        "edge_01",
        "edge/edge_01/input",
    )

    assert claimed["run_id"] == request["run_id"]
    assert repository.assigned_edge_node_ids(
        request["run_id"], exclude_task_id="sd_02_tk_0001"
    ) == {"edge_01"}
