# -*- coding: utf-8 -*-
"""有界包级模型队列 + 超时 + 熔断（Windows 侧）。

队列中的一个元素只对应一个 PerceptionResult。模型或降级路线的结果只能回填
该任务身份，不进行跨包映射。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .config import EdgeModelConfig
from .contracts import (
    REASON_BREAKER_OPEN,
    REASON_MODEL_BUSY,
    REASON_MODEL_INFERENCE_FAILED,
    REASON_MODEL_INFERENCE_TIMEOUT,
    REASON_MODEL_INPUT_INVALID,
    REASON_MODEL_OUTPUT_INVALID,
    REASON_MODEL_UNAVAILABLE,
    REASON_QUEUE_FULL,
    REASON_QUEUE_TIMEOUT,
    REASON_TOTAL_TIMEOUT,
    EdgeResult,
    PacketInferenceTask,
)
from .model_client import ModelInferResult

FULL_POLICY_REJECT = "reject"
FULL_POLICY_REPLACE = "replace"


@dataclass
class SubmitResult:
    accepted: bool
    fallback_tasks: List[Tuple[PacketInferenceTask, str]] = field(default_factory=list)


class ModelTaskQueue:
    def __init__(self, max_waiting_requests: int, full_policy: str, clock=time.monotonic):
        self.capacity = max_waiting_requests
        self.full_policy = full_policy
        self._clock = clock
        self._cond = threading.Condition()
        self._pending: List[PacketInferenceTask] = []
        self._inflight = 0
        self.stopped = False
        self.max_observed_queued = 0

    def submit(self, task: PacketInferenceTask) -> SubmitResult:
        with self._cond:
            if task.submit_ts is None:
                task.submit_ts = self._clock()
            if self.stopped:
                return SubmitResult(False, [(task, "STOPPED")])
            if len(self._pending) < self.capacity:
                self._pending.append(task)
                self.max_observed_queued = max(self.max_observed_queued, len(self._pending))
                self._cond.notify()
                return SubmitResult(True)
            if self.full_policy == FULL_POLICY_REPLACE and self._pending:
                replaced = self._pending.pop(0)
                self._pending.append(task)
                self._cond.notify()
                return SubmitResult(True, [(replaced, REASON_QUEUE_FULL)])
            return SubmitResult(False, [(task, REASON_QUEUE_FULL)])

    def get(self, timeout_s: float = 0.2) -> Optional[PacketInferenceTask]:
        with self._cond:
            while not self._pending and not self.stopped:
                self._cond.wait(timeout_s)
            if not self._pending:
                return None
            task = self._pending.pop(0)
            self._inflight += 1
            return task

    def done(self):
        with self._cond:
            self._inflight -= 1
            self._cond.notify_all()

    def idle(self) -> bool:
        with self._cond:
            return not self._pending and self._inflight == 0

    def wait_until_idle(self, timeout_s: float = 5.0) -> bool:
        deadline = self._clock() + timeout_s
        with self._cond:
            while (self._pending or self._inflight > 0) and self._clock() < deadline:
                self._cond.wait(0.05)
            return not self._pending and self._inflight == 0

    def stop(self):
        with self._cond:
            self.stopped = True
            self._cond.notify_all()


class InferenceWorker:
    def __init__(self, queue: ModelTaskQueue,
                 infer_fn: Callable[..., ModelInferResult], cfg: EdgeModelConfig,
                 on_model: Callable[[PacketInferenceTask, EdgeResult, float, float, float, bool, str], None],
                 on_fallback: Callable[[PacketInferenceTask, str, float, Optional[float], Optional[str], Optional[str]], None],
                 clock=time.monotonic, poll_s: float = 0.05):
        self.queue = queue
        self.infer_fn = infer_fn
        self.cfg = cfg
        self.on_model = on_model
        self.on_fallback = on_fallback
        self._clock = clock
        self._poll_s = poll_s
        self._consecutive_failures = 0
        self._breaker_open_until: Optional[float] = None
        self.breaker_state = "closed"
        self._thread = threading.Thread(target=self._loop, daemon=True, name="edge-model-worker")
        self._running = False

    def start(self):
        self._running = True
        self._thread.start()

    def stop(self, join_s: float = 5.0):
        self._running = False
        self.queue.stop()
        self._thread.join(timeout=join_s)

    def _note_failure(self):
        if not self.cfg.breaker.enabled:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.breaker.consecutive_failure_threshold:
            self._breaker_open_until = self._clock() + self.cfg.breaker.recovery_probe_interval_s
            self.breaker_state = "open"

    def _note_success(self):
        self._consecutive_failures = 0
        self._breaker_open_until = None
        self.breaker_state = "closed"

    def _breaker_allows_model(self) -> bool:
        if not self.cfg.breaker.enabled or self._breaker_open_until is None:
            return True
        if self._clock() < self._breaker_open_until:
            return False
        self._breaker_open_until = None
        return True

    def _loop(self):
        while self._running or not self.queue.idle():
            task = self.queue.get(timeout_s=self._poll_s)
            if task is None:
                continue
            try:
                self._process(task)
            finally:
                self.queue.done()

    @staticmethod
    def _map_error_to_reason(error: Optional[str]) -> str:
        return {
            "MODEL_BUSY": REASON_MODEL_BUSY,
            "MODEL_UNAVAILABLE": REASON_MODEL_UNAVAILABLE,
            "MODEL_INFERENCE_FAILED": REASON_MODEL_INFERENCE_FAILED,
            "MODEL_INFERENCE_TIMEOUT": REASON_MODEL_INFERENCE_TIMEOUT,
            "MODEL_INPUT_INVALID": REASON_MODEL_INPUT_INVALID,
            "MODEL_OUTPUT_INVALID": REASON_MODEL_OUTPUT_INVALID,
        }.get(error or "", REASON_MODEL_INFERENCE_FAILED)

    def _process(self, task: PacketInferenceTask):
        cfg = self.cfg
        now = self._clock()
        submit_ts = task.submit_ts if task.submit_ts is not None else now
        queue_wait_ms = (now - submit_ts) * 1000.0
        breaker_state = self.breaker_state
        total_deadline = submit_ts + cfg.timeout.total_ms / 1000.0
        reserve_s = cfg.timeout.fallback_reserve_ms / 1000.0

        if not self._breaker_allows_model():
            self.on_fallback(task, REASON_BREAKER_OPEN, queue_wait_ms, None, breaker_state, None)
            return
        if queue_wait_ms > cfg.timeout.queue_wait_ms:
            self.on_fallback(task, REASON_QUEUE_TIMEOUT, queue_wait_ms, None, breaker_state, None)
            return
        remaining_to_deadline = total_deadline - now
        if remaining_to_deadline <= reserve_s:
            self.on_fallback(task, REASON_TOTAL_TIMEOUT, queue_wait_ms, None, breaker_state,
                             "no_time_for_model_or_fallback")
            return
        model_budget_ms = min(cfg.timeout.inference_ms,
                              (remaining_to_deadline - reserve_s) * 1000.0)
        if model_budget_ms <= 0:
            self.on_fallback(task, REASON_TOTAL_TIMEOUT, queue_wait_ms, None, breaker_state,
                             "no_time_for_model")
            return

        result = self._run_infer(task, model_budget_ms)
        total_ms = (self._clock() - submit_ts) * 1000.0

        if result.timed_out:
            self._note_failure()
            self.on_fallback(task, REASON_MODEL_INFERENCE_TIMEOUT, queue_wait_ms,
                             result.latency_ms, breaker_state, None)
            return
        if not result.success:
            self._note_failure()
            self.on_fallback(task, self._map_error_to_reason(result.error), queue_wait_ms,
                             result.latency_ms, breaker_state, result.error)
            return
        if result.edge is None or result.request_id != task.request_id:
            self._note_failure()
            self.on_fallback(task, REASON_MODEL_OUTPUT_INVALID, queue_wait_ms,
                             result.latency_ms, breaker_state, "request_id_or_edge_invalid")
            return
        if self._clock() > total_deadline:
            self._note_failure()
            self.on_fallback(task, REASON_TOTAL_TIMEOUT, queue_wait_ms,
                             result.latency_ms, breaker_state, "model_result_late")
            return

        self._note_success()
        self.on_model(task, result.edge, queue_wait_ms, result.latency_ms or 0.0,
                      total_ms, False, result.edge.model_version)

    def _run_infer(self, task: PacketInferenceTask,
                   model_budget_ms: float) -> ModelInferResult:
        """在子线程执行 HTTP 推理；超预算后不交付迟到结果。"""

        budget_s = model_budget_ms / 1000.0
        holder: dict = {}
        t0 = self._clock()

        def _run():
            try:
                holder["result"] = self.infer_fn(
                    task.perception,
                    int(model_budget_ms),
                    request_id=task.request_id,
                    remaining_timeout_ms=model_budget_ms,
                )
                holder["ok"] = True
            except Exception as exc:  # noqa: BLE001
                holder["ok"] = False
                holder["err"] = exc

        thread = threading.Thread(target=_run, daemon=True, name="infer-http")
        thread.start()
        thread.join(budget_s + 0.05)
        if thread.is_alive():
            return ModelInferResult(success=False, timed_out=True,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_TIMEOUT")
        if not holder.get("ok"):
            return ModelInferResult(success=False, timed_out=False,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_FAILED")
        return holder["result"]
