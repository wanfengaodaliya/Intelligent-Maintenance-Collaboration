# -*- coding: utf-8 -*-
"""H2/H4: InferenceWorker 异常护栏 + 固定线程池 + 协作式取消验证。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

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


def test_suggestion_worker_stop_drains_pending_results() -> None:
    """遗留修复3：stop 前排空队列，在途设备级结果不丢建议。"""
    from edge_runtime.suggestion_worker import SuggestionWorker

    outbox: list[dict] = []
    worker = SuggestionWorker(
        llm_client=None,
        outbox=type("Outbox", (), {"enqueue": staticmethod(lambda p: outbox.append(p))})(),
        publisher=None,
    )
    # 不 start：submit 入队后直接 stop，全部建议必须在排空阶段处理完。
    for revision in (1, 2):
        worker.submit(
            SimpleNamespace(
                device_id="device_01", task_id="task_01",
                decision_round_id="round-01", revision=revision,
                status=SimpleNamespace(value="FINAL"), final_state="normal",
                final_action_grade=0, confidence=0.9,
            )
        )
    worker.stop()
    assert sorted(p["device_result_revision"] for p in outbox) == [1, 2]
