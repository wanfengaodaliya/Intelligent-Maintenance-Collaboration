from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from core.diagnosis_contracts import BearingDecisionResult, BearingLifecycleStatus, RoundClosureReason
from device_decision import DeviceDecisionRoundRepository, aggregate_device_round
from edge_runtime.coordinator import EdgeRuntimeCoordinator
from edge_runtime.device_result_outbox import (
    DEAD_LETTER,
    DeviceResultOutbox,
    PUBLISHED,
    PUBLISHING,
    RETRY_WAIT,
)
from edge_runtime.maintenance import EdgeMaintenanceWorker
from result_lifecycle import BearingResultRepository


def _bearing(bearing_id: str, *, grade: int) -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id=f"bearing_round_01_{bearing_id}_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}", decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}", lifecycle_state=BearingLifecycleStatus.FINAL_EDGE,
        bearing_state="normal", confidence=0.9, data_quality_score=0.9,
        risk_level="low", action_grade=grade,
        recommended_action=("continue_operation", "enhanced_monitoring", "scheduled_inspection", "urgent_intervention", "shutdown")[grade],
        decision_source="EDGE", review_status="NOT_REQUIRED", degraded=False,
        edge_result_id=f"edge_{bearing_id}", cloud_result_id=None, model_version="edge_model_v1",
        created_at_ns=1, edge_accepted_at_ns=2,
    )


def _device_result(tmp_path):
    repository = DeviceDecisionRoundRepository(tmp_path / "edge.db")
    aggregate = aggregate_device_round(
        (_bearing("bearing_a", grade=1), _bearing("bearing_b", grade=1)),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=10,
    )
    return replace(aggregate, result_id="device_result_001")


def test_maintenance_worker_recovers_after_round_error() -> None:
    attempts: list[int] = []

    def run_round(now_ns: int):
        attempts.append(now_ns)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"rounds": len(attempts)}

    worker = EdgeMaintenanceWorker(run_round, interval_seconds=1.0)

    assert worker.run_round_once() is True
    health = worker.health()
    assert health["errors_total"] == 1
    assert health["last_error_code"] == "RuntimeError"

    # 单轮异常不能阻止下一轮继续执行。
    assert worker.run_round_once() is True
    health = worker.health()
    assert health["rounds_total"] == 1
    assert health["errors_total"] == 1
    assert health["last_error_code"] is None
    assert health["last_summary"] == {"rounds": 2}


def test_maintenance_worker_thread_survives_errors_and_stops(tmp_path) -> None:
    import time
    import threading

    done = threading.Event()
    calls = {"total": 0}

    def run_round(now_ns: int):
        calls["total"] += 1
        if calls["total"] == 1:
            raise RuntimeError("first round fails")
        if calls["total"] >= 3:
            done.set()
        return {"calls": calls["total"]}

    worker = EdgeMaintenanceWorker(run_round, interval_seconds=0.02)
    worker.start()
    try:
        assert done.wait(timeout=5.0), "worker must keep running after an error"
        assert worker.running
    finally:
        worker.stop(timeout_seconds=2.0)
    assert worker.running is False
    assert worker.health()["errors_total"] == 1


def test_outbox_publishes_each_version_exactly_once(tmp_path) -> None:
    published: list[dict] = []
    outbox = DeviceResultOutbox(tmp_path / "edge.db", published.append, max_attempts=3)
    result = _device_result(tmp_path)

    assert outbox.enqueue(result) is True
    assert outbox.enqueue(result) is True  # 重复入队幂等
    assert outbox.run_once(0) == 1
    assert outbox.run_once(1) == 0  # 已发布版本不再发送

    assert len(published) == 1
    assert published[0]["result_id"] == "device_result_001"
    health = outbox.health()
    assert health[PUBLISHED] == 1
    assert health["backlog"] == 0


def test_outbox_retries_with_backoff_then_dead_letters(tmp_path) -> None:
    def failing(payload):
        raise RuntimeError("mqtt down")

    outbox = DeviceResultOutbox(tmp_path / "edge.db", failing, max_attempts=2)
    outbox.enqueue(_device_result(tmp_path))

    assert outbox.run_once(0) == 0
    assert outbox.health()[RETRY_WAIT] == 1
    # 退避窗口内不到期。
    assert outbox.run_once(100_000_000) == 0
    # 超过退避后第二次尝试失败，进入死信。
    assert outbox.run_once(10_000_000_000) == 0
    health = outbox.health()
    assert health[DEAD_LETTER] == 1
    assert health["backlog"] == 0


def test_outbox_recovers_publishing_entries_on_startup(tmp_path) -> None:
    published: list[dict] = []
    database = tmp_path / "edge.db"
    first = DeviceResultOutbox(database, published.append, max_attempts=5)
    first.enqueue(_device_result(tmp_path))
    # 模拟进程在发送中途退出：条目停留在 PUBLISHING。
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("UPDATE device_result_outbox SET status=?", (PUBLISHING,))
    connection.commit()
    connection.close()

    second = DeviceResultOutbox(database, published.append, max_attempts=5)
    assert second.health()[RETRY_WAIT] == 1
    assert second.run_once(0) == 1
    assert len(published) == 1


class _RecordingScheduler:
    def __init__(self) -> None:
        self.reports: list[dict] = []

    def report_status(self, payload):
        self.reports.append(payload)
        return {}


def test_maintenance_round_is_decoupled_from_status_reporting() -> None:
    scheduler = _RecordingScheduler()
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01",
        ingress=object(),
        cache=object(),
        pipeline=SimpleNamespace(queue_length=0),
        scheduler=scheduler,
    )

    summary = coordinator.run_maintenance_once(now_ns=10)

    assert scheduler.reports == []
    assert summary["finished_at_ns"] >= 0
    assert summary["device_results_published"] == 0

    coordinator.report_node_status()
    assert len(scheduler.reports) == 1
    assert scheduler.reports[0]["edge_node_id"] == "edge_01"
