from __future__ import annotations

import sqlite3

import pytest

from summary_service.outbox import ArbitrationOutbox
from summary_service.repository import SummaryRepository
from summary_service.service import SummaryService


ACTIONS = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}


def bearing_result(
    bearing_id: str,
    edge_node_id: str,
    action_grade: int,
    *,
    result_suffix: str | None = None,
) -> dict[str, object]:
    suffix = result_suffix or bearing_id
    return {
        "result_id": f"result_{suffix}",
        "device_id": "machine_01",
        "task_id": f"sd_{bearing_id[-2:]}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{bearing_id[-2:]}",
        "edge_node_id": edge_node_id,
        "decision_round_id": f"round_{suffix}",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "bearing_state": "fault" if action_grade >= 2 else "normal",
        "risk_level": "high" if action_grade >= 2 else "low",
        "action_grade": action_grade,
        "recommended_action": ACTIONS[action_grade],
        "confidence": 0.9,
        "data_quality_score": 0.8,
        "model_version": "model-test",
        "created_at_ns": 100,
    }


def test_aggregates_three_bearings_and_detects_only_cross_edge_conflict(tmp_path):
    published: list[dict[str, object]] = []
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, publish_window_result=lambda value: published.append(dict(value)))

    assert service.ingest(bearing_result("bearing_01", "edge_01", 0)) is None
    assert service.ingest(bearing_result("bearing_02", "edge_01", 3)) is None
    result = service.ingest(bearing_result("bearing_03", "edge_02", 1))

    assert result is not None
    assert result["result_status"] == "PENDING_ARBITRATION"
    assert result["has_conflict"] is True
    assert result["cross_edge_pair_count"] == 2
    assert result["conflict_pair_count"] == 1
    assert result["max_grade_gap"] == 2
    assert result["action_grades_by_edge"] == {"edge_01": [0, 3], "edge_02": [1]}
    assert result["final_action_grade"] == 3
    assert len(result["source_results"]) == 3
    assert published == [result]


def test_duplicate_delivery_is_idempotent(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    payloads = [
        bearing_result("bearing_01", "edge_01", 0),
        bearing_result("bearing_02", "edge_02", 0),
        bearing_result("bearing_03", "edge_01", 0),
    ]
    for payload in payloads:
        service.ingest(payload)

    repeated = service.ingest(payloads[-1])

    assert repeated is not None
    assert len(repository.list_window_results()) == 1
    assert repository.metrics()["eligible_windows"] == 1


def test_same_edge_window_is_excluded_from_formal_metrics(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    for bearing_id, grade in (("bearing_01", 0), ("bearing_02", 3)):
        service.ingest(bearing_result(bearing_id, "edge_01", grade))
    result = service.ingest(bearing_result("bearing_03", "edge_01", 1))

    assert result is not None
    assert result["result_status"] == "INCOMPLETE"
    assert result["has_conflict"] is False
    assert result["incomplete_reason"] == "INSUFFICIENT_EDGE_DIVERSITY"
    metrics = repository.metrics()
    assert metrics["total_windows"] == 1
    assert metrics["eligible_windows"] == 0
    assert metrics["incomplete_windows"] == 1
    assert metrics["conflict_rate"] == 0.0


def test_conflict_is_uploaded_once_and_acknowledged(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    for payload in (
        bearing_result("bearing_01", "edge_01", 0),
        bearing_result("bearing_02", "edge_01", 3),
        bearing_result("bearing_03", "edge_02", 1),
    ):
        service.ingest(payload)

    requests: list[dict[str, object]] = []

    def transport(payload):
        requests.append(dict(payload))
        return {"arbitration_result_id": "cloud_result_01", "status": "RESOLVED"}

    outbox = ArbitrationOutbox(repository, transport, now_ns=lambda: 1_000)

    assert outbox.run_due() == 1
    assert outbox.run_due() == 0
    assert len(requests) == 1
    assert len(requests[0]["bearing_results"]) == 3
    assert requests[0]["comparison"] == {
        "max_cross_edge_grade_gap": 2,
        "conflicting_pair_count": 1,
    }

    with sqlite3.connect(repository.database_path) as connection:
        state = connection.execute(
            "SELECT state FROM summary_arbitration_outbox"
        ).fetchone()[0]
    assert state == "ACKNOWLEDGED"
    metrics = repository.metrics()
    assert metrics["arbitration_upload_windows"] == 1
    assert metrics["arbitration_acknowledged_windows"] == 1
    assert metrics["arbitration_upload_success_rate"] == pytest.approx(1.0)


def test_same_result_slot_with_different_payload_is_rejected(tmp_path):
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository)
    original = bearing_result("bearing_01", "edge_01", 0)
    service.ingest(original)
    changed = dict(original)
    changed["result_id"] = "different_result_id"
    changed["confidence"] = 0.7

    with pytest.raises(ValueError, match="identity conflicts"):
        service.ingest(changed)


def test_missing_bearing_closes_as_incomplete_after_timeout(tmp_path):
    now = {"value": 100}
    repository = SummaryRepository(tmp_path / "summary.db")
    service = SummaryService(repository, now_ns=lambda: now["value"])
    service.ingest(bearing_result("bearing_01", "edge_01", 0))
    service.ingest(bearing_result("bearing_02", "edge_02", 1))

    assert service.close_expired(now_ns=109, timeout_ns=10) == 0
    assert service.close_expired(now_ns=110, timeout_ns=10) == 1

    result = repository.list_window_results()[0]
    assert result["result_status"] == "INCOMPLETE"
    assert result["missing_bearing_ids"] == ["bearing_03"]
    assert result["excluded_from_formal_metrics"] is True
    assert repository.metrics()["incomplete_windows"] == 1
