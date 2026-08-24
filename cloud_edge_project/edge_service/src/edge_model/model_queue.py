# -*- coding: utf-8 -*-
"""有界包级模型队列 + 超时 + 熔断（Windows 侧）。

队列中的一个元素只对应一个 PerceptionResult。模型或降级路线的结果只能回填
该任务身份，不进行跨包映射。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

_logger = logging.getLogger(__name__)


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
        # 阶段 7：满载可观测——因队列满而被拒绝/置换的提交累计数。
        self.queue_full_total = 0

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
            self.queue_full_total += 1
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

    @property
    def waiting_count(self) -> int:
        with self._cond:
            return len(self._pending)

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
        # 多消费线程共享同一队列与熔断状态：可变状态统一经 _state_lock 保护。
        self._consumer_count = max(1, int(cfg.inference_workers))
        self._state_lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_open_until: Optional[float] = None
        self.breaker_state = "closed"
        self._threads: List[threading.Thread] = []
        self._running = False
        # H4：固定推理线程池，替代每任务新建线程，从结构上消除超时后的僵尸线程。
        self._executor: Optional[ThreadPoolExecutor] = None
        # H2：worker 护栏观测——循环内单任务异常计数与最近错误，线程本身不死。
        self.loop_error_count = 0
        self.last_loop_error: Optional[str] = None

    def start(self):
        self._running = True
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                # 每个消费线程各占一个执行位 + 少量余量：逻辑超时被弃置的任务
                # 在协作式取消检查点退出前仍占线程，余量避免其挤占新任务。
                max_workers=self._consumer_count + 2,
                thread_name_prefix="edge-infer",
            )
        for index in range(self._consumer_count):
            thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="edge-model-worker-%d" % index,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, join_s: float = 5.0):
        self._running = False
        self.queue.stop()
        for thread in self._threads:
            thread.join(timeout=join_s)
        self._threads = []
        executor = self._executor
        self._executor = None
        if executor is not None:
            # 协作式取消已通过 cancel_event 通知在途推理；wait=False 立即返回，
            # 在途任务在下一个检查点自行退出，不阻塞关闭。
            executor.shutdown(wait=False, cancel_futures=True)

    @property
    def worker_alive(self) -> bool:
        """H2：全部消费线程存活才视为存活（任一死亡=降容，健康检查应暴露）。"""
        return bool(self._threads) and all(t.is_alive() for t in self._threads)

    @property
    def consumer_count(self) -> int:
        """当前配置的消费线程数（供 /health 观测实际并行度）。"""
        return self._consumer_count

    def _note_failure(self):
        if not self.cfg.breaker.enabled:
            return
        with self._state_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.cfg.breaker.consecutive_failure_threshold:
                self._breaker_open_until = self._clock() + self.cfg.breaker.recovery_probe_interval_s
                self.breaker_state = "open"

    def _note_success(self):
        with self._state_lock:
            self._consecutive_failures = 0
            self._breaker_open_until = None
            self.breaker_state = "closed"

    def _breaker_allows_model(self) -> bool:
        with self._state_lock:
            if not self.cfg.breaker.enabled or self._breaker_open_until is None:
                return True
            if self._clock() < self._breaker_open_until:
                return False
            self._breaker_open_until = None
            return True

    def _loop(self):
        # H2：单任务异常不得杀死 worker 线程——记日志、计数并继续消费下一个任务。
        while self._running or not self.queue.idle():
            task = self.queue.get(timeout_s=self._poll_s)
            if task is None:
                continue
            try:
                self._process(task)
            except Exception as exc:  # noqa: BLE001
                with self._state_lock:
                    self.loop_error_count += 1
                    self.last_loop_error = "%s: %s" % (type(exc).__name__, exc)
                _logger.exception("edge-model-worker 任务处理异常(线程继续运行): %s", exc)
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
        """在固定线程池执行推理；超预算后设置取消事件并不交付迟到结果。

        H4：线程数恒定（cfg.inference_workers），超时任务通过 cancel_event 触发
        协作式取消，从结构上消除"每任务新建线程 + 超时弃置"的僵尸线程堆积。
        """

        budget_s = model_budget_ms / 1000.0
        t0 = self._clock()
        cancel_event = threading.Event()
        executor = self._executor
        if executor is None:
            # 防御分支：线程池未就绪（正常生命周期不可达）时同步执行以交付结果。
            return _run_infer_direct(self.infer_fn, task, model_budget_ms)

        infer_task = getattr(self.infer_fn, "infer_task", None)
        if callable(infer_task):
            future = executor.submit(
                _run_infer_task, infer_task, task, int(model_budget_ms), cancel_event
            )
        else:
            future = executor.submit(
                _run_infer_call, self.infer_fn, task, int(model_budget_ms), cancel_event
            )
        try:
            return future.result(timeout=budget_s + 0.05)
        except TimeoutError:
            # 逻辑超时：通知协作式取消；迟到结果由 request_id 校验在 _process 丢弃。
            cancel_event.set()
            return ModelInferResult(success=False, timed_out=True,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_TIMEOUT")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("edge-model-worker 推理执行异常: %s", exc)
            return ModelInferResult(success=False, timed_out=False,
                                    latency_ms=(self._clock() - t0) * 1000.0,
                                    error="MODEL_INFERENCE_FAILED")


def _run_infer_task(infer_task, task, budget_ms: int, cancel_event) -> ModelInferResult:
    """线程池入口：local_h5 任务级钩子，携带协作式取消事件。"""
    try:
        return infer_task(task, budget_ms, cancel_event=cancel_event)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("edge-infer infer_task 执行异常: %s", exc)
        return ModelInferResult(
            success=False, timed_out=False, error="MODEL_INFERENCE_FAILED",
            request_id=getattr(task, "request_id", None),
        )


def _run_infer_call(infer_fn, task, budget_ms: int, cancel_event) -> ModelInferResult:
    """线程池入口：HTTP 路线，携带协作式取消事件。"""
    try:
        return infer_fn(
            task.perception,
            budget_ms,
            request_id=task.request_id,
            remaining_timeout_ms=budget_ms,
            cancel_event=cancel_event,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("edge-infer infer 执行异常: %s", exc)
        return ModelInferResult(
            success=False, timed_out=False, error="MODEL_INFERENCE_FAILED",
            request_id=getattr(task, "request_id", None),
        )


def _run_infer_direct(infer_fn, task, budget_ms: float) -> ModelInferResult:
    """防御性同步执行（线程池未就绪时），不携带取消事件。"""
    infer_task = getattr(infer_fn, "infer_task", None)
    if callable(infer_task):
        return infer_task(task, int(budget_ms))
    return infer_fn(
        task.perception,
        int(budget_ms),
        request_id=task.request_id,
        remaining_timeout_ms=budget_ms,
    )
