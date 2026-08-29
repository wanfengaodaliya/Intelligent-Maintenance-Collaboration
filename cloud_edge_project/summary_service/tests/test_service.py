from __future__ import annotations

import pytest

from summary_service.repository import SummaryRepository
from summary_service.service import SummaryService


LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low"),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low"),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high"),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high"),
}


def bearing_result(
    bearing_id: str,
    edge_node_id: str,
    state: str = "normal",
    action_level: int = 0,
    *,
    run_id: str = "run_01",
) -> dict:
    suffix = bearing_id[-2:]
    result_id = f"result_{run_id}_{suffix}"
    probabilities, risk_level = LEVEL_PROBS[action_level]
    return {
        "result_id": result_id,
        "device_id": "machine_01",
        "task_id": f"sd_{suffix}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{suffix}",
        "edge_node_id": edge_node_id,
        "decision_round_id": f"round_{suffix}",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "bearing_state": state,
        "risk_level": risk_level,
        "confidence": 0.9,
        "data_quality_score": 1.0,
        "model_version": "model-test",
        "created_at_ns": 100,
        "run_id": run_id,
        "class_probabilities": probabilities,
    }


def test_first_result_waits_and_second_result_finalizes_the_window(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)

    assert service.ingest(bearing_result("bearing_01", "edge_01", "normal")) is None

    result = service.ingest(bearing_result("bearing_02", "edge_02", "normal"))

    assert result is not None
    assert result["result_status"] == "FINAL"
    assert result["revision"] == 1
    assert result["final_state"] == "normal"


def test_state_mismatch_window_waits_for_arbitration(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    service.ingest(bearing_result("bearing_01", "edge_01", "normal"))

    result = service.ingest(bearing_result("bearing_02", "edge_02", "fault", 3))

    assert result["result_status"] == "PENDING_ARBITRATION"
    assert result["has_conflict"] is True
    assert result["arbitration_status"] == "PENDING"


def test_duplicate_delivery_does_not_reaggregate(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    payloads = [
        bearing_result("bearing_01", "edge_01", "fault", 3),
        bearing_result("bearing_02", "edge_02", "fault", 3),
    ]
    first = service.ingest(payloads[0])
    second = service.ingest(payloads[1])
    assert second is not None

    repeated = service.ingest(payloads[1])

    assert repeated == second
    assert repository.list_window_results()[0]["revision"] == 1
    metrics = repository.metrics()
    assert metrics["counters"]["duplicate_bearing_result_messages"] == 1


def test_same_edge_window_is_incomplete_and_excluded(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    service.ingest(bearing_result("bearing_01", "edge_01", "normal", 0))
    service.ingest(bearing_result("bearing_02", "edge_01", "fault", 3))

    result = repository.list_window_results()[0]

    assert result["result_status"] == "INCOMPLETE"
    assert result["incomplete_reason"] == "INSUFFICIENT_EDGE_DIVERSITY"
    metrics = repository.metrics()
    assert metrics["eligible_windows"] == 0
    assert metrics["incomplete_windows"] == 1


def test_unknown_edge_node_is_counted_and_rejected_before_persistence(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)

    with pytest.raises(ValueError, match="unsupported edge_node_id"):
        service.ingest(bearing_result("bearing_02", "edge_03", "fault", 3))

    assert repository.list_window_results() == []
    assert repository.load_expired_open_windows(cutoff_ns=2_000) == []
    assert (
        repository.metrics()["counters"]["unknown_edge_node_results"] == 1
    )


def test_missing_bearing_closes_as_incomplete_after_timeout(tmp_path):
    now = {"value": 100}
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, now_ns=lambda: now["value"])
    service.ingest(bearing_result("bearing_01", "edge_01", "normal", 0))

    assert service.close_expired(now_ns=109, timeout_ns=10) == 0
    assert service.close_expired(now_ns=110, timeout_ns=10) == 1

    result = repository.list_window_results()[0]
    assert result["result_status"] == "INCOMPLETE"
    assert result["missing_bearing_ids"] == ["bearing_02"]
    assert result["window_close_duration_ns"] == 10


def test_late_completion_of_incomplete_window_bumps_revision(tmp_path):
    now = {"value": 100}
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, now_ns=lambda: now["value"])
    service.ingest(bearing_result("bearing_01", "edge_01", "normal", 0))
    service.close_expired(now_ns=110, timeout_ns=10)
    assert repository.list_window_results()[0]["result_status"] == "INCOMPLETE"

    now["value"] = 150
    completed = service.ingest(bearing_result("bearing_02", "edge_02", "normal", 1))

    assert completed is not None
    assert completed["result_status"] == "FINAL"
    assert completed["revision"] == 2
    assert completed["final_state"] == "normal"
    assert completed["window_close_duration_ns"] == 50


def test_late_completion_with_mismatch_enters_arbitration(tmp_path):
    now = {"value": 100}
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, now_ns=lambda: now["value"])
    service.ingest(bearing_result("bearing_01", "edge_01", "normal", 0))
    service.close_expired(now_ns=110, timeout_ns=10)

    now["value"] = 150
    completed = service.ingest(bearing_result("bearing_02", "edge_02", "fault", 3))

    assert completed["result_status"] == "PENDING_ARBITRATION"
    assert completed["revision"] == 2
    assert completed["arbitration_status"] == "PENDING"


def test_redelivery_after_incomplete_close_keeps_the_window_frozen(tmp_path):
    now = {"value": 100}
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, now_ns=lambda: now["value"])
    first = bearing_result("bearing_01", "edge_01", "normal", 0)
    service.ingest(first)
    service.close_expired(now_ns=110, timeout_ns=10)
    assert repository.list_window_results()[0]["result_status"] == "INCOMPLETE"

    result = service.ingest(first)

    assert result is not None
    assert result["result_status"] == "INCOMPLETE"
    assert result["revision"] == 1


def test_settled_window_is_not_reopened_by_late_messages(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    service.ingest(bearing_result("bearing_01", "edge_01", "fault", 3))
    settled = service.ingest(bearing_result("bearing_02", "edge_02", "fault", 3))

    # A redelivered message returns the settled window untouched.
    repeated = service.ingest(bearing_result("bearing_02", "edge_02", "fault", 3))

    assert repeated == settled
    assert repository.list_window_results()[0]["revision"] == 1


def test_conflicting_result_for_occupied_slot_is_rejected(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    service.ingest(bearing_result("bearing_01", "edge_01", "normal", 0))

    # Same bearing slot from the other node must not masquerade as a second node.
    with pytest.raises(ValueError):
        service.ingest(bearing_result("bearing_01", "edge_02", "fault", 3))


@pytest.mark.parametrize(
    "expected_bearing_ids",
    [(), ("bearing_01", "bearing_01"), ("bearing_01", "bearing_04")],
)
def test_rejects_invalid_expected_bearings(tmp_path, expected_bearing_ids):
    repository = SummaryRepository(tmp_path / "summary.db")
    with pytest.raises(ValueError):
        SummaryService(repository, expected_bearing_ids=expected_bearing_ids)


@pytest.mark.parametrize(
    "expected_edge_node_ids",
    [(), ("edge_01", "edge_01")],
)
def test_rejects_invalid_expected_edge_nodes(tmp_path, expected_edge_node_ids):
    repository = SummaryRepository(tmp_path / "summary.db")
    with pytest.raises(ValueError):
        SummaryService(repository, expected_edge_node_ids=expected_edge_node_ids)


def test_different_runs_do_not_share_windows(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)

    first = service.ingest(
        bearing_result("bearing_01", "edge_01", "normal", 0, run_id="run_01")
    )
    assert first is None
    first = service.ingest(
        bearing_result("bearing_02", "edge_02", "normal", 0, run_id="run_01")
    )
    assert first is not None

    second = service.ingest(
        bearing_result("bearing_01", "edge_01", "fault", 3, run_id="run_02")
    )
    assert second is None
    second = service.ingest(
        bearing_result("bearing_02", "edge_02", "fault", 3, run_id="run_02")
    )
    assert second is not None

    results = repository.list_window_results()
    assert len(results) == 2
    assert {result["run_id"] for result in results} == {"run_01", "run_02"}
    assert len({result["summary_window_id"] for result in results}) == 2
