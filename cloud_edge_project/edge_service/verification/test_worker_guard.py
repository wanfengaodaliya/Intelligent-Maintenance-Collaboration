# -*- coding: utf-8 -*-
"""H2/H4: InferenceWorker 异常护栏 + 固定线程池 + 协作式取消验证。"""

from __future__ import annotations

import threading
import time

from edge_model.config import EdgeModelConfig
from edge_model.contracts import EdgeResult, PacketInferenceTask
from edge_model.local_h5_client import LocalH5ModelClient
from edge_model.model_client import ModelInferResult
from edge_model.model_queue import InferenceWorker, ModelTaskQueue


def _task(request_id: str = "req-1") -> PacketInferenceTask:
    return PacketInferenceTask(
        request_id=request_id, device_id="d", bearing_id="b", task_id="t",
        packet_id="p", sender_id="s", sequence_number=1, perception={},
    )


def _ok_result(request_id: str) -> ModelInferResult:
    return ModelInferResult(
        success=True,
        edge=EdgeResult(
            edge_result="normal", confidence=0.9,
            edge_risk_level="low", model_version="v1",
        ),
        request_id=request_id,
    )


def test_worker_survives_on_model_exception() -> None:
    """H2：回调链抛异常时 worker 线程不死，计数并继续消费。"""
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.inference_workers = 1
    queue = ModelTaskQueue(4, "reject")

    def infer_fn(perception, timeout_ms, request_id=None,
                 remaining_timeout_ms=None, cancel_event=None):
        return _ok_result(request_id)

    def on_model(*args):
        raise RuntimeError("completion handler exploded")

    worker = InferenceWorker(
        queue, infer_fn, cfg,
        on_model=on_model,
        on_fallback=lambda *a, **k: None,
    )
    worker.start()
    try:
        queue.submit(_task())
        assert queue.wait_until_idle(timeout_s=5.0)
        assert worker.worker_alive is True
        assert worker.loop_error_count >= 1
        assert worker.last_loop_error is not None
    finally:
        worker.stop()


def test_timeout_triggers_cooperative_cancel() -> None:
    """H4：逻辑超时后设置 cancel_event，协作式取消让推理线程及时退出。"""
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.inference_workers = 1
    cfg.timeout.queue_wait_ms = 100
    cfg.timeout.inference_ms = 40
    cfg.timeout.total_ms = 200
    cfg.timeout.fallback_reserve_ms = 20
    queue = ModelTaskQueue(4, "reject")
    state = {"cancelled": threading.Event()}
    fallback: list = []

    def slow_infer(perception, timeout_ms, request_id=None,
                   remaining_timeout_ms=None, cancel_event=None):
        while not (cancel_event is not None and cancel_event.is_set()):
            time.sleep(0.005)
        state["cancelled"].set()
        raise RuntimeError("cancelled")

    worker = InferenceWorker(
        queue, slow_infer, cfg,
        on_model=lambda *a, **k: None,
        on_fallback=lambda *a, **k: fallback.append(a),
    )
    worker.start()
    try:
        queue.submit(_task())
        assert queue.wait_until_idle(timeout_s=5.0)
        assert state["cancelled"].wait(2.0), "cancel_event 未被设置"
    finally:
        worker.stop()
    assert len(fallback) == 1


def test_inference_workers_config_default_and_validation() -> None:
    cfg = EdgeModelConfig()
    assert cfg.inference_workers == 1
    cfg.inference_workers = 0
    assert "inference_workers" in "\n".join(cfg.validate())


def test_multiple_consumers_share_queue_and_record_work() -> None:
    """多个消费者必须能并行取同一个队列中的任务，且暴露各自处理数。"""
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    cfg.inference_workers = 2
    cfg.timeout.queue_wait_ms = 2_000
    cfg.timeout.inference_ms = 1_000
    cfg.timeout.total_ms = 4_000
    queue = ModelTaskQueue(4, "reject")
    barrier = threading.Barrier(2)
    started_lock = threading.Lock()
    started_ids: set[str] = set()
    both_started = threading.Event()

    def infer_fn(perception, timeout_ms, request_id=None,
                 remaining_timeout_ms=None, cancel_event=None):
        with started_lock:
            started_ids.add(request_id)
            if len(started_ids) == 2:
                both_started.set()
        try:
            barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError:
            pass
        return _ok_result(request_id)

    worker = InferenceWorker(
        queue, infer_fn, cfg,
        on_model=lambda *a, **k: None,
        on_fallback=lambda *a, **k: None,
    )
    worker.start()
    try:
        assert worker.consumer_count == 2
        queue.submit(_task("req-1"))
        queue.submit(_task("req-2"))
        assert both_started.wait(1.0), "两个消费者没有并行开始推理"
        assert queue.wait_until_idle(timeout_s=5.0)
        consumers = worker.consumer_snapshot
        assert len(consumers) == 2
        assert sum(item["processed"] for item in consumers) == 2
    finally:
        barrier.abort()
        worker.stop()


def test_successful_inference_latency_snapshot_is_aggregated() -> None:
    """健康指标应聚合成功本地推理的耗时，且不需要保存原始包。"""
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = "http"
    queue = ModelTaskQueue(4, "reject")
    latencies = iter((10.0, 30.0))

    def infer_fn(perception, timeout_ms, request_id=None,
                 remaining_timeout_ms=None, cancel_event=None):
        result = _ok_result(request_id)
        result.latency_ms = next(latencies)
        return result

    worker = InferenceWorker(
        queue, infer_fn, cfg,
        on_model=lambda *a, **k: None,
        on_fallback=lambda *a, **k: None,
    )
    worker.start()
    try:
        queue.submit(_task("req-1"))
        queue.submit(_task("req-2"))
        assert queue.wait_until_idle(timeout_s=5.0)
        assert worker.inference_latency_snapshot == {
            "count": 2,
            "mean": 20.0,
            "p50": 10.0,
            "p95": 30.0,
            "max": 30.0,
        }
    finally:
        worker.stop()


def test_infer_task_forwards_cancel_event_to_model() -> None:
    """H4：local_h5 的 infer_task 把 cancel_event 透传给模型 run。"""
    client = LocalH5ModelClient()
    seen: dict = {}

    class FakeModel:
        model_version = "fake-v1"

        def run(self, task, cancel_event=None):
            seen["cancel_event"] = cancel_event
            return EdgeResult(
                edge_result="normal", confidence=0.9,
                edge_risk_level="low", model_version="fake-v1",
            )

    client.attach_model_for_test(FakeModel())
    event = threading.Event()
    result = client.infer_task(_task(), cancel_event=event)
    assert result.success is True
    assert seen["cancel_event"] is event


def test_http_infer_pre_cancelled_skips_request() -> None:
    """遗留修复1：HTTP 路线 cancel_event 已置位时不发起请求，直接按超时放弃。"""
    from edge_model.config import ModelClientConfig
    from edge_model.model_client import ModelClient

    requested: list[str] = []

    client = ModelClient(ModelClientConfig(base_url="http://127.0.0.1:9"))

    def _fail_request(path, payload=None, read_timeout_s=None):
        requested.append(path)
        raise AssertionError("cancelled request must not reach HTTP layer")

    client._request_json = _fail_request
    cancel = threading.Event()
    cancel.set()
    result = client.infer({}, inference_timeout_ms=50, cancel_event=cancel)
    assert result.success is False
    assert result.timed_out is True
    assert requested == []


def test_edge_runtime_does_not_expose_suggestion_generation() -> None:
    from edge_runtime.coordinator import EdgeRuntimeCoordinator

    assert not hasattr(EdgeRuntimeCoordinator, "submit_device_suggestion")
