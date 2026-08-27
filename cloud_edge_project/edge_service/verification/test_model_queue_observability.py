# -*- coding: utf-8 -*-
"""阶段 7：模型队列满载可观测性（queue_full_total / max_observed_queued / 快照）。

- reject 策略：满载提交被拒且计数累计，峰值深度等于容量；
- replace 策略：满载提交置换最老任务并计数；
- pipeline.queue_snapshot()：/health 暴露的容量与满载指标字段完整。
"""
from __future__ import annotations

from types import SimpleNamespace

from edge_model.config import EdgeModelConfig, QueueConfig
from edge_model.contracts import PacketInferenceTask, REASON_QUEUE_FULL
from edge_model.model_queue import FULL_POLICY_REJECT, FULL_POLICY_REPLACE, ModelTaskQueue
from edge_model.pipeline import EdgeModelPipeline


def _task(sequence: int) -> PacketInferenceTask:
    return PacketInferenceTask(
        request_id="req_%03d" % sequence,
        device_id="device_q",
        bearing_id="bearing_01",
        task_id="task_q",
        packet_id="pkt_%03d" % sequence,
        sender_id="sender_01",
        sequence_number=sequence,
        perception={},
    )


def test_reject_policy_counts_full_submissions() -> None:
    queue = ModelTaskQueue(2, FULL_POLICY_REJECT, clock=lambda: 0.0)
    assert queue.submit(_task(1)).accepted
    assert queue.submit(_task(2)).accepted
    result = queue.submit(_task(3))
    assert not result.accepted
    assert result.fallback_tasks[0][1] == REASON_QUEUE_FULL
    assert queue.queue_full_total == 1
    queue.submit(_task(4))
    assert queue.queue_full_total == 2
    assert queue.max_observed_queued == 2
    assert queue.waiting_count == 2


def test_replace_policy_displaces_oldest_and_counts() -> None:
    queue = ModelTaskQueue(2, FULL_POLICY_REPLACE, clock=lambda: 0.0)
    queue.submit(_task(1))
    queue.submit(_task(2))
    result = queue.submit(_task(3))
    assert result.accepted
    displaced = result.fallback_tasks[0][0]
    assert displaced.packet_id == "pkt_001"  # 最老任务被置换
    assert result.fallback_tasks[0][1] == REASON_QUEUE_FULL
    assert queue.queue_full_total == 1
    assert queue.waiting_count == 2


def test_pipeline_queue_snapshot_exposes_capacity_and_full_metrics() -> None:
    cfg = EdgeModelConfig(diagnostic_backend="http",
                          queue=QueueConfig(max_waiting_requests=2,
                                            full_policy=FULL_POLICY_REJECT))
    pipeline = EdgeModelPipeline(
        cfg, SimpleNamespace(infer=lambda *a, **k: None), None,
        on_run_record=lambda r: None, on_packet_result=lambda r: None)
    snapshot = pipeline.queue_snapshot()
    assert snapshot["waiting"] == 0
    assert snapshot["capacity"] == 2
    assert snapshot["full_policy"] == FULL_POLICY_REJECT
    assert snapshot["max_observed_queued"] == 0
    assert snapshot["queue_full_total"] == 0
    assert snapshot["consumer_count"] == 1
    assert snapshot["consumers"] == [{"id": 0, "alive": False, "processed": 0}]
    assert snapshot["inference_latency_ms"]["count"] == 0
    pipeline.queue.submit(_task(1))
    pipeline.queue.submit(_task(2))
    pipeline.queue.submit(_task(3))  # 满载拒绝
    snapshot = pipeline.queue_snapshot()
    assert snapshot["waiting"] == 2
    assert snapshot["max_observed_queued"] == 2
    assert snapshot["queue_full_total"] == 1
