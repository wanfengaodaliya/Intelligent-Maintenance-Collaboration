# -*- coding: utf-8 -*-
"""有界模型任务队列 + 单推理 worker + 三层超时 + 熔断（Windows 侧）。

逻辑已验证。生产版差异：推理是通过 model_client 发 HTTP 调用（不是进程内 torch）。

队列语义：max_waiting_requests 只统计「等待中」，正在推理的 1 条不占额度；
队列满策略 reject（新窗口直接降级）或 replace（替换尚未开始的旧窗口，正在
推理的不打断）。

超时语义（文档冻结）：
    queue_wait_ms   进入队列 → 获得执行资格
    inference_ms    调用模型服务 → 等待响应超时（逻辑超时，不终止已开始的 generate）
    total_ms        窗口诊断任务创建 → 最终结果完成

熔断由本侧（Windows 调用侧）维护；恢复探测期内放行一次真实推理。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .code_fallback import CodeFallbackRunner
from .config import EdgeModelConfig
from .contracts import (
    REASON_BREAKER_OPEN,
    REASON_MODEL_BUSY,
    REASON_MODEL_INFERENCE_FAILED,
    REASON_MODEL_INFERENCE_TIMEOUT,
    REASON_MODEL_OUTPUT_INVALID,
    REASON_MODEL_UNAVAILABLE,
    REASON_QUEUE_FULL,
    REASON_QUEUE_TIMEOUT,
    REASON_TOTAL_TIMEOUT,
    EdgeResult,
    RunRecord,
    WindowAggregate,
)
from .model_client import ModelInferResult

FULL_POLICY_REJECT = "reject"
FULL_POLICY_REPLACE = "replace"


@dataclass
class SubmitResult:
    accepted: bool
    fallback_windows: List[Tuple[WindowAggregate, str]] = field(default_factory=list)


class ModelTaskQueue:
    def __init__(self, max_waiting_requests: int, full_policy: str, clock=time.monotonic):
        self.capacity = max_waiting_requests
        self.full_policy = full_policy
        self._clock = clock
        self._cond = threading.Condition()
        self._pending: List[WindowAggregate] = []
        self._inflight = 0
        self.stopped = False
        self.max_observed_queued = 0

    def submit(self, window: WindowAggregate) -> SubmitResult:
        with self._cond:
            if self.stopped:
                return SubmitResult(accepted=False, fallback_windows=[(window, "STOPPED")])
            if len(self._pending) < self.capacity:
                window.submit_ts = self._clock()
                self._pending.append(window)
                self.max_observed_queued = max(self.max_observed_queued, len(self._pending))
                self._cond.notify()
                return SubmitResult(accepted=True)
            if self.full_policy == FULL_POLICY_REPLACE and self._pending:
                replaced = self._pending.pop(0)
                window.submit_ts = self._clock()
                self._pending.append(window)
                self._cond.notify()
                return SubmitResult(accepted=True, fallback_windows=[(replaced, REASON_QUEUE_FULL)])
            return SubmitResult(accepted=False, fallback_windows=[(window, REASON_QUEUE_FULL)])

    def get(self, timeout_s: float = 0.2) -> Optional[WindowAggregate]:
        with self._cond:
            while not self._pending and not self.stopped:
                self._cond.wait(timeout_s)
            if not self._pending:
                return None
            w = self._pending.pop(0)
            self._inflight += 1
            return w

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
    def __init__(self, queue: ModelTaskQueue, infer_fn: Callable[[dict, int], ModelInferResult],
                 fallback: CodeFallbackRunner, cfg: EdgeModelConfig,
                 on_model: Callable[[WindowAggregate, EdgeResult, float, float, float, bool, str], None],
                 on_fallback: Callable[[WindowAggregate, str, float, Optional[float], Optional[str], Optional[str]], None],
                 clock=time.monotonic, poll_s: float = 0.05):
        self.queue = queue
        self.infer_fn = infer_fn
        self.fallback = fallback
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

    # ---- 熔断 ----
    def _note_failure(self):
        if not self.cfg.breaker.enabled:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.breaker.consecutive_failure_threshold:
            self._breaker_open_until = self._clock() + self.cfg.breaker.recovery_probe_interval_s
            self.breaker_state = "open"

    def _note_success(self):
        self._consecutive_failures = 0
        if self.breaker_state == "open":
            self.breaker_state = "closed"
        self._breaker_open_until = None

    def _breaker_allows_model(self) -> bool:
        if not self.cfg.breaker.enabled:
            return True
        if self._breaker_open_until is None:
            return True
        if self._clock() < self._breaker_open_until:
            return False
        self._breaker_open_until = None  # 探测期到，放行一次真实推理
        return True

    # ---- 主循环 ----
    def _loop(self):
        while self._running or not self.queue.idle():
            window = self.queue.get(timeout_s=self._poll_s)
            if window is None:
                continue
            try:
                self._process(window)
            finally:
                self.queue.done()

    @staticmethod
    def _map_error_to_reason(error: Optional[str]) -> str:
        """把模型服务返回的错误码映射为内部降级原因。"""
        return {
            "MODEL_BUSY": REASON_MODEL_BUSY,
            "MODEL_UNAVAILABLE": REASON_MODEL_UNAVAILABLE,
            "MODEL_INFERENCE_FAILED": REASON_MODEL_INFERENCE_FAILED,
            "MODEL_INFERENCE_TIMEOUT": REASON_MODEL_INFERENCE_TIMEOUT,
            "MODEL_OUTPUT_INVALID": REASON_MODEL_OUTPUT_INVALID,
        }.get(error or "", REASON_MODEL_INFERENCE_FAILED)

    def _process(self, window: WindowAggregate):
        cfg = self.cfg
        now = self._clock()
        submit_ts = window.submit_ts if window.submit_ts is not None else now
        queue_wait_ms = (now - submit_ts) * 1000.0
        breaker_state = self.breaker_state
        total_deadline = submit_ts + cfg.timeout.total_ms / 1000.0
        reserve_s = cfg.timeout.fallback_reserve_ms / 1000.0

        if not self._breaker_allows_model():
            self.on_fallback(window, REASON_BREAKER_OPEN, queue_wait_ms, None, breaker_state, None)
            return
        if queue_wait_ms > cfg.timeout.queue_wait_ms:
            self.on_fallback(window, REASON_QUEUE_TIMEOUT, queue_wait_ms, None, breaker_state, None)
            return
        # 剩余总时间：不足模型+降级预留 → 两条路线都失败
        remaining_to_deadline = total_deadline - now
        if remaining_to_deadline <= reserve_s:
            self.on_fallback(window, REASON_TOTAL_TIMEOUT, queue_wait_ms, None, breaker_state,
                             "no_time_for_model_or_fallback")
            return
        # 模型等待预算 = min(推理超时, 剩余总时间 - 降级预留)
        model_budget_ms = min(cfg.timeout.inference_ms, (remaining_to_deadline - reserve_s) * 1000.0)
        if model_budget_ms <= 0:
            self.on_fallback(window, REASON_TOTAL_TIMEOUT, queue_wait_ms, None, breaker_state,
                             "no_time_for_model")
            return

        result = self._run_infer(window, model_budget_ms)
        total_ms = (self._clock() - submit_ts) * 1000.0

        if result.timed_out:
            self._note_failure()
            self.on_fallback(window, REASON_MODEL_INFERENCE_TIMEOUT, queue_wait_ms,
                             result.latency_ms, breaker_state, None)
            return
        if not result.success:
            self._note_failure()
            self.on_fallback(window, self._map_error_to_reason(result.error), queue_wait_ms,
                             result.latency_ms, breaker_state, result.error)
            return
        if result.edge is None:
            self._note_failure()
            self.on_fallback(window, REASON_MODEL_OUTPUT_INVALID, queue_wait_ms,
                             result.latency_ms, breaker_state, result.error)
            return

        # 模型结果必须在总截止时间内交付；超截止 → 改走降级（但降级也超时 → 两条路线失败）
        now = self._clock()
        if now > total_deadline:
            self._note_failure()
            self.on_fallback(window, REASON_TOTAL_TIMEOUT, queue_wait_ms,
                             result.latency_ms, breaker_state, "model_result_late_no_time_for_fallback")
            return
        self._note_success()
        self.on_model(window, result.edge, queue_wait_ms, result.latency_ms or 0.0,
                      total_ms, False, result.edge.model_version)

    def _run_infer(self, window: WindowAggregate, model_budget_ms: float) -> ModelInferResult:
        """在子线程执行 HTTP 推理，join 超时判定（逻辑超时）。

        model_budget_ms 同时作为：HTTP 读取超时、join 超时、服务端 remaining_timeout。
        超预算即视为推理超时，改走降级；不交付迟到结果。
        """
        budget_s = model_budget_ms / 1000.0
        request_id = "%s-%d-%d" % (window.sender_id, window.window_id, int(self._clock() * 1000))
        holder: dict = {}
        t0 = self._clock()

        def _run():
            try:
                holder["result"] = self.infer_fn(window.payload, int(model_budget_ms),
                                                 request_id=request_id,
                                                 remaining_timeout_ms=model_budget_ms)
                holder["ok"] = True
            except Exception as exc:  # noqa: BLE001
                holder["ok"] = False
                holder["err"] = exc

        t = threading.Thread(target=_run, daemon=True, name="infer-http")
        t.start()
        t.join(budget_s + 0.05)
        if t.is_alive():
            return ModelInferResult(success=False, timed_out=True,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_TIMEOUT")
        if not holder.get("ok"):
            return ModelInferResult(success=False, timed_out=False,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_FAILED")
        return holder["result"]
