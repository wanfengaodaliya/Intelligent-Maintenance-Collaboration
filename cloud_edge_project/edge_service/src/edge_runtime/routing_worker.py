# -*- coding: utf-8 -*-
"""H2: 有界异步路由 worker 池。

把「快照落盘 + Scheduler HTTP POST」移出 completion dispatcher 主循环：

- dispatcher 单线程仍负责：bearing 结果入 outbox、提交 routing job、
  以及按提交序号串行回放 V1.2 apply_edge_result；
- N 个 worker 线程并发执行 store.save + HTTP POST（两者线程安全：
  CloudReviewStore.save 自带锁，JsonHttpClient 无共享状态）；
- 结果按提交序号重排后，以 RouteReplay 事件交还 dispatcher 线程串行
  回放，保证 V1.2/SQLite 状态推进不并发。

指标：depth / oldest_wait_ns / in_flight / accepted / replayed / failed /
overflow。停机：stop() 先停止接收新 job，等待 worker 排空在途任务并
完成全部回放；未启动（如测试直连）时 submit 退化为同步路由 + 同步回放。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, NamedTuple, Optional


class RoutingJob(NamedTuple):
    seq: int
    completion: Any
    raw_packet: Any
    diagnosis_window: Any
    edge_result: Any
    task: Any
    enqueued_at_ns: int


class RouteReplay(NamedTuple):
    """routing worker 完成后交还 dispatcher 线程串行回放的事件。"""

    seq: int
    completion: Any
    task: Any
    edge_result: Any
    route_decision: Optional[dict]


class RoutingWorkerPool:
    def __init__(
        self,
        route_fn: Callable[[Any, Any, Any], Any],
        replay_fn: Callable[[RouteReplay], None],
        *,
        worker_count: int = 4,
        queue_size: int = 256,
        clock_ns=time.time_ns,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be positive")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._route_fn = route_fn
        self._replay_fn = replay_fn
        self._worker_count = worker_count
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._stopped = False
        self._accepting = True
        self._next_seq = 1
        self._next_replay = 1
        self._pending: dict[int, tuple[RoutingJob, Any]] = {}
        self._inflight = 0
        self.accepted_total = 0
        self.replayed_total = 0
        self.failed_total = 0
        self.overflow_total = 0

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._accepting = True
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"edge-routing-worker-{index}",
                daemon=True,
            )
            for index in range(self._worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def stop(self, *, timeout_seconds: float = 15.0) -> None:
        """先停止接收新 job，再排空 worker 并完成全部按序回放。

        停止后 submit 一律返回 False（不再退化为同步路由）；未启动时
        调用无副作用。
        """
        if not self._started:
            return
        with self._lock:
            self._accepting = False
        # 每 worker 一个停止哨兵；worker 处理完在途 job 后消费哨兵退出。
        for _ in self._workers:
            self._queue.put(None)
        deadline = time.monotonic() + timeout_seconds
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                worker.join(timeout=remaining)
        # 防御：worker 超时未完成时残留的 pending 仍按序回放，避免丢路由结果。
        with self._lock:
            while self._next_replay in self._pending:
                replay_job, replay_decision = self._pending.pop(self._next_replay)
                self._next_replay += 1
                self.replayed_total += 1
                self._invoke_replay(replay_job, replay_decision)
        self._workers = []
        self._started = False
        self._stopped = True

    # ---- submission ----------------------------------------------------

    def submit(
        self,
        completion: Any,
        raw_packet: Any,
        diagnosis_window: Any,
        edge_result: Any,
        task: Any,
    ) -> bool:
        """提交 routing job；返回 False 表示未接收（停止中或队列满）。

        未启动（如测试未 start 的直连装配）时退化为同步路由 + 同步回放，
        语义与旧同步路径一致；已停止时一律拒绝。
        """
        if self._stopped:
            return False
        if not self._started:
            self.accepted_total += 1
            self.replayed_total += 1
            decision = self._route_fn(completion, raw_packet, diagnosis_window)
            self._invoke_replay(
                RoutingJob(0, completion, raw_packet, diagnosis_window,
                           edge_result, task, 0),
                decision,
            )
            return True
        with self._lock:
            if not self._accepting:
                return False
            seq = self._next_seq
            self._next_seq += 1
        job = RoutingJob(
            seq, completion, raw_packet, diagnosis_window, edge_result, task,
            self._clock_ns(),
        )
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self.overflow_total += 1
            return False
        self.accepted_total += 1
        return True

    # ---- metrics -------------------------------------------------------

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def alive(self) -> bool:
        """启动后全部 worker 线程存活才视为 true；未启动视为 false。"""
        if not self._started:
            return False
        return bool(self._workers) and all(
            worker.is_alive() for worker in self._workers
        )

    @property
    def oldest_wait_ns(self) -> Optional[int]:
        """当前排队 job 中最老的等待时长（未开始路由）；队列空时为 None。"""
        with self._lock:
            if self._queue.empty():
                return None
            now = self._clock_ns()
            oldest = None
            for job in list(self._queue.queue):
                wait = now - job.enqueued_at_ns
                if oldest is None or wait > oldest:
                    oldest = wait
            return oldest

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    # ---- internals -----------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                with self._lock:
                    self._inflight += 1
                try:
                    decision = self._route_fn(
                        job.completion, job.raw_packet, job.diagnosis_window
                    )
                except Exception:  # noqa: BLE001
                    # route_fn 自身已有兜底；此处防御性计数，避免 worker 崩溃。
                    self.failed_total += 1
                    decision = None
                finally:
                    with self._lock:
                        self._inflight -= 1
                self._on_job_done(job, decision)
            finally:
                self._queue.task_done()

    def _on_job_done(self, job: RoutingJob, decision: Any) -> None:
        """乱序完成的结果按提交序号重排后连续回放。

        回放回调在锁内执行，保证同一时刻只有一个线程（按 seq 递增顺序）
        调用 replay_fn；replay_fn 只做非阻塞入队（dispatcher.submit），
        不会反向获取本池锁，无死锁环。
        """
        with self._lock:
            self._pending[job.seq] = (job, decision)
            while self._next_replay in self._pending:
                replay_job, replay_decision = self._pending.pop(self._next_replay)
                self._next_replay += 1
                self.replayed_total += 1
                self._invoke_replay(replay_job, replay_decision)

    def _invoke_replay(self, job: RoutingJob, decision: Any) -> None:
        try:
            self._replay_fn(
                RouteReplay(
                    job.seq, job.completion, job.task, job.edge_result, decision
                )
            )
        except Exception:  # noqa: BLE001
            # replay 处理自身有兜底；防御性吞掉，避免 worker 线程崩坏。
            pass
