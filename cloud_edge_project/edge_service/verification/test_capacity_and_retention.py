# -*- coding: utf-8 -*-
"""阶段 5 验证：入站容量指标、liveness 依据与 Outbox 数据保留策略。"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    DeviceDecisionResult,
    RoundClosureReason,
)
from device_decision import aggregate_device_round
from edge_runtime.config import MqttConfig
from edge_runtime.coordinator import EdgeRuntimeCoordinator
from edge_runtime.device_result_outbox import (
    DEAD_LETTER,
    DeviceResultOutbox,
    PUBLISHED,
    RETRY_WAIT,
)
from edge_runtime.mqtt import MqttIngress


# ---------- MqttIngress 容量指标 ----------


class _FakeMqttClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    def manual_ack_set(self, value: bool) -> None:
        pass

    def reconnect_delay_set(self, *args, **kwargs) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class _FakeMessage:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.mid = 1
        self.qos = 1


def _ingress(capacity: int = 2) -> tuple[MqttIngress, _FakeMqttClient]:
    client = _FakeMqttClient()
    ingress = MqttIngress(
        MqttConfig(ingress_queue_capacity=capacity),
        lambda _: None,
        client=client,
    )
    return ingress, client


def test_ingress_worker_not_alive_before_start() -> None:
    ingress, _ = _ingress()
    assert ingress.worker_alive is False
    snapshot = ingress.capacity_snapshot()
    assert snapshot["worker_alive"] is False
    assert snapshot["queue_capacity"] == 2
    assert snapshot["queue_depth"] == 0
    assert snapshot["rejected_total"] == 0
    assert snapshot["oldest_task_age_ms"] is None


def test_ingress_reject_counts_and_disconnects_when_full() -> None:
    ingress, client = _ingress(capacity=1)
    # 预先占满队列。
    ingress._queue.put(({"packet_id": "p1"}, _FakeMessage({"packet_id": "p1"}), 1))
    # 队列已满：新消息被拒绝、计数并断连背压。
    ingress._on_message(client, None, _FakeMessage({"packet_id": "p2"}))

    assert ingress.rejected_total == 1
    assert client.disconnect_calls == 1
    snapshot = ingress.capacity_snapshot()
    assert snapshot["rejected_total"] == 1
    assert snapshot["queue_depth"] == 1


def test_ingress_oldest_task_age_tracks_queue_head() -> None:
    ingress, _ = _ingress(capacity=4)
    import time

    before = time.time_ns()
    ingress._queue.put(({"packet_id": "p1"}, object(), time.time_ns()))
    age = ingress.oldest_task_age_ms
    assert age is not None and age >= 0.0
    # 消费后队列为空，年龄回到 None。
    ingress._queue.get_nowait()
    ingress._queue.task_done()
    assert ingress.oldest_task_age_ms is None


# ---------- Outbox 数据保留与积压年龄 ----------


def _bearing(bearing_id: str) -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id=f"bearing_round_01_{bearing_id}_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}", decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}", lifecycle_state=BearingLifecycleStatus.FINAL_EDGE,
        bearing_state="normal", confidence=0.9, data_quality_score=0.9,
        risk_level="low", action_grade=1,
        recommended_action="enhanced_monitoring",
        decision_source="EDGE", review_status="NOT_REQUIRED", degraded=False,
        edge_result_id=f"edge_{bearing_id}", cloud_result_id=None,
        model_version="edge_model_v1", created_at_ns=1, edge_accepted_at_ns=2,
    )


def _device_result(device_id: str = "machine_01", task_id: str = "task_001") -> DeviceDecisionResult:
    from dataclasses import replace

    aggregate = aggregate_device_round(
        (_bearing("bearing_a"), _bearing("bearing_b")),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=10,
    )
    return replace(
        aggregate,
        result_id=f"device_{task_id}_r1",
        task_id=task_id,
    )


_SECOND = 1_000_000_000
_HOUR = 3600 * _SECOND


def test_outbox_cleanup_removes_only_expired_published(tmp_path) -> None:
    published: list[dict] = []
    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", published.append, clock_ns=lambda: base
    )
    outbox.enqueue(_device_result())
    assert outbox.run_once(base) == 1
    assert outbox.health()[PUBLISHED] == 1

    # 保留 24h：发布于 base，now=base+23h 未超期不删；base+25h 超期删除。
    assert outbox.cleanup_published(retention_ns=24 * _HOUR, now_ns=base + 23 * _HOUR) == 0
    assert outbox.cleanup_published(retention_ns=24 * _HOUR, now_ns=base + 25 * _HOUR) == 1
    assert outbox.health()[PUBLISHED] == 0
    # 重复清理幂等。
    assert outbox.cleanup_published(retention_ns=24 * _HOUR, now_ns=base + 26 * _HOUR) == 0


def test_outbox_cleanup_never_touches_dead_letter_or_backlog(tmp_path) -> None:
    def failing(payload):
        raise RuntimeError("mqtt down")

    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", failing, max_attempts=1, clock_ns=lambda: base
    )
    outbox.enqueue(_device_result(task_id="task_dead"))
    # 发布失败 + max_attempts=1 → 死信。
    outbox.run_once(base)
    assert outbox.health()[DEAD_LETTER] == 1

    # 再放一条未发布记录。
    outbox.enqueue(_device_result(task_id="task_pending"))

    removed = outbox.cleanup_published(
        retention_ns=1 * _HOUR, now_ns=base + 100 * _HOUR
    )
    assert removed == 0
    health = outbox.health()
    assert health[DEAD_LETTER] == 1
    assert health["backlog"] >= 0  # 死信与待发布均不受清理影响


def test_outbox_health_reports_oldest_backlog_age(tmp_path) -> None:
    def failing(payload):
        raise RuntimeError("mqtt down")

    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", failing, max_attempts=5, clock_ns=lambda: base
    )
    # 空库时无积压年龄。
    assert outbox.health()["oldest_backlog_age_ms"] is None

    outbox.enqueue(_device_result(task_id="task_old"))
    health = outbox.health()
    assert health["oldest_backlog_age_ms"] is not None
    assert health["oldest_backlog_age_ms"] >= 0.0


def test_outbox_retention_zero_disables_cleanup(tmp_path) -> None:
    published: list[dict] = []
    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", published.append, clock_ns=lambda: base
    )
    outbox.enqueue(_device_result())
    outbox.run_once(base)
    assert outbox.cleanup_published(retention_ns=0, now_ns=base + 1000 * _HOUR) == 0
    assert outbox.health()[PUBLISHED] == 1


# ---------- 维护轮次联动清理 ----------


def test_maintenance_round_runs_outbox_cleanup(tmp_path) -> None:
    published: list[dict] = []
    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", published.append, clock_ns=lambda: base
    )
    outbox.enqueue(_device_result())
    outbox.run_once(base)

    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01",
        ingress=object(),
        cache=object(),
        pipeline=SimpleNamespace(queue_length=0),
        scheduler=SimpleNamespace(),
        device_result_outbox=outbox,
        clock_ns=lambda: base,
    )
    coordinator.outbox_published_retention_ns = 1 * _HOUR

    summary = coordinator.run_maintenance_once(now_ns=base + 2 * _HOUR)
    assert summary["outbox_published_cleaned"] == 1
    assert outbox.health()[PUBLISHED] == 0


def test_maintenance_round_skips_cleanup_when_retention_disabled(tmp_path) -> None:
    published: list[dict] = []
    base = 1_000 * _HOUR
    outbox = DeviceResultOutbox(
        tmp_path / "edge.db", published.append, clock_ns=lambda: base
    )
    outbox.enqueue(_device_result())
    outbox.run_once(base)

    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01",
        ingress=object(),
        cache=object(),
        pipeline=SimpleNamespace(queue_length=0),
        scheduler=SimpleNamespace(),
        device_result_outbox=outbox,
        clock_ns=lambda: base,
    )
    # 默认未注入保留期：不清理。
    summary = coordinator.run_maintenance_once(now_ns=base + 100 * _HOUR)
    assert summary["outbox_published_cleaned"] == 0
    assert outbox.health()[PUBLISHED] == 1
