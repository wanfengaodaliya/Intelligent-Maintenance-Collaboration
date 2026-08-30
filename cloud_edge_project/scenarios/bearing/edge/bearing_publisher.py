# -*- coding: utf-8 -*-
"""H3-ASYNC: 本地 bearing 结果异步发布器。

H3-FIX 把 outbox 发布移到推理 worker 线程，虽然把
model_finished -> outbox_enqueued 从 P95 1.6s 压到 ~50ms，
但 SQLite 写入(~50ms/包)阻塞单消费者推理 worker，使模型队列在
多 sender 并发到达下积压，触发 QUEUE_TIMEOUT 降级雪崩
（warmup7 实测 171/240 降级，warmup6 旧架构零降级）。

本类把发布彻底移出推理 worker：submit 仅 O(1) 入队，独立单线程
串行执行 SQLite 写入。队列满或停止中时丢弃本次发布，由 dispatcher
内 H1-FIX 段落的幂等 enqueue 兑底（outbox 按 result_id 幂等，语义
不丢，仅 enqueue 延迟回升到 dispatcher 路径水平）。

指标：depth / accepted / published / failed / overflow。
停机：stop() 先停止接收新 job，等待在途 job 全部发布完成；
未启动时 submit 退化为同步发布（测试直连装配语义不变）。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional


class BearingPublisher:
    """单线程 FIFO 发布本地 bearing 结果；队列满时丢弃(由 H1-FIX 幂等兑底)。"""

    def __init__(
        self,
        publish_fn: Callable[[Any], None],
        *,
        queue_size: int = 256,
        on_error: Optional[Callable[[Any, Exception], None]] = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._publish_fn = publish_fn
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._on_error = on_error
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._stopped = False
        self._accepting = True
        self.accepted_total = 0
        self.published_total = 0
        self.failed_total = 0
        self.overflow_total = 0

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._accepting = True
        self._thread = threading.Thread(
            target=self._loop, name="edge-bearing-publisher", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        """先停止接收新 job，再排空在途 job 后退出线程。

        停止后 submit 一律返回 False；未启动时调用无副作用。
        """
        if not self._started:
            return
        with self._lock:
            self._accepting = False
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        self._thread = None
        self._started = False
        self._stopped = True

    # ---- submission ----------------------------------------------------

    def submit(self, completion: Any) -> bool:
        """提交发布 job；返回 False 表示未接收（停止中或队列满）。

        未启动（如测试直连装配）时退化为同步发布；已停止时一律拒绝。
        队列满时丢弃 job——dispatcher 内 H1-FIX 幂等 enqueue 兑底。
        """
        if self._stopped:
            return False
        if not self._started:
            self.accepted_total += 1
            try:
                self._publish_fn(completion)
            except Exception as exc:  # noqa: BLE001
                # publish_fn 自身静默兜底；此处防御性吞掉，避免同步调用方崩坏。
                self.failed_total += 1
                if self._on_error is not None:
                    try:
                        self._on_error(completion, exc)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                self.published_total += 1
            return True
        with self._lock:
            if not self._accepting:
                return False
        try:
            self._queue.put_nowait(completion)
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
    def alive(self) -> bool:
        """启动后线程存活才视为 true；未启动视为 false。"""
        if not self._started:
            return False
        return self._thread is not None and self._thread.is_alive()

    # ---- internals -----------------------------------------------------

    def _loop(self) -> None:
        while True:
            completion = self._queue.get()
            try:
                if completion is None:
                    return
                try:
                    self._publish_fn(completion)
                except Exception as exc:  # noqa: BLE001
                    # publish_fn 自身静默兜底；此处防御性计数，避免线程崩坏。
                    self.failed_total += 1
                    if self._on_error is not None:
                        try:
                            self._on_error(completion, exc)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    self.published_total += 1
            finally:
                self._queue.task_done()
