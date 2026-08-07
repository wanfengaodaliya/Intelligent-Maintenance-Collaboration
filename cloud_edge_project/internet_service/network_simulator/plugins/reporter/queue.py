"""Small bounded FIFO that drops the oldest report when full."""

from __future__ import annotations

from collections import deque
from threading import Condition
import time

from .schemas import NetworkReport


class DropOldestReportQueue:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be at least one")
        self._capacity = capacity
        self._items: deque[NetworkReport] = deque()
        self._condition = Condition()
        self._closed = False

    def put(self, report: NetworkReport) -> NetworkReport | None:
        with self._condition:
            if self._closed:
                raise RuntimeError("report queue is closed")
            dropped = self._items.popleft() if len(self._items) == self._capacity else None
            self._items.append(report)
            self._condition.notify()
            return dropped

    def get(self, timeout: float | None = None) -> NetworkReport | None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._items:
                if self._closed:
                    return None
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._items.popleft()

    def close(self, *, discard: bool = False) -> tuple[NetworkReport, ...]:
        with self._condition:
            discarded = tuple(self._items) if discard else ()
            if discard:
                self._items.clear()
            self._closed = True
            self._condition.notify_all()
            return discarded

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def __len__(self) -> int:
        with self._condition:
            return len(self._items)
