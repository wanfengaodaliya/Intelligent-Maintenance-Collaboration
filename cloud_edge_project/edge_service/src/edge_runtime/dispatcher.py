# -*- coding: utf-8 -*-
"""H1: 完成事件分发线程——数据面(推理 worker)与控制面(路由/落盘/上报)隔离。

推理 worker 完成后只做一次 O(1) 入队，路由 HTTP / 窗口落盘 / v12 状态推进 /
raw sample 采集等阻塞控制面 I/O 全部移到本线程串行执行，单线程 FIFO 保序，
SQLite 写也随之串行化（顺带收窄 M2 的 SELECT-then-INSERT 竞态）。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Optional

_logger = logging.getLogger(__name__)


class CompletionDispatcher:
    """单线程 FIFO 分发完成事件；队列满时降级为调用方内联执行(宁可退化不可丢)。"""

    def __init__(
        self,
        handler: Callable[[Any], None],
        *,
        enabled: bool = True,
        queue_size: int = 256,
        on_error: Optional[Callable[[Any, Exception], None]] = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.handler = handler
        self.enabled = enabled
        self.capacity = queue_size
        self.on_error = on_error
        self._queue: deque = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.overflow_total = 0
        self.processed_total = 0
        self.error_total = 0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="edge-completion-dispatcher", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout_seconds)

    @property
    def alive(self) -> bool:
        # 未启用时无独立线程，不存在"线程死亡"问题，视为存活。
        if not self.enabled:
            return True
        return self._thread is not None and self._thread.is_alive()

    @property
    def queue_size(self) -> int:
        with self._cond:
            return len(self._queue)

    def submit(self, item: Any) -> None:
        if not self.enabled:
            self._dispatch(item)
            return
        with self._cond:
            if self._thread is None or self._stop.is_set():
                # 未启动或已停止：调用方内联执行，结果生命周期仍被推进。
                self._dispatch(item)
                return
            if len(self._queue) < self.capacity:
                self._queue.append(item)
                self._cond.notify()
                return
            self.overflow_total += 1
        # 溢出：降级为调用方内联执行，完成事件驱动结果生命周期，宁可偶发退化不可丢。
        _logger.warning(
            "completion dispatcher 队列满, 内联执行(overflow=%d)", self.overflow_total
        )
        self._dispatch(item)

    def _loop(self) -> None:
        # 停止后仍排空队列（drain）：只要还有在途完成事件就继续处理。
        while True:
            with self._cond:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait(0.2)
                if not self._queue and self._stop.is_set():
                    return
                item = self._queue.popleft()
            self.processed_total += 1
            self._dispatch(item)

    def _dispatch(self, item: Any) -> None:
        try:
            self.handler(item)
        except Exception as exc:  # noqa: BLE001
            self.error_total += 1
            if self.on_error is not None:
                try:
                    self.on_error(item, exc)
                except Exception:  # noqa: BLE001
                    pass
            _logger.exception("completion dispatcher 处理完成事件异常: %s", exc)
