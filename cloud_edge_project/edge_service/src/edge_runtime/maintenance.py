# -*- coding: utf-8 -*-
"""Independent background worker that advances edge business maintenance rounds."""
# 该模块提供独立的后台维护线程，只推进业务超时、重试、恢复与待发布任务。

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping, Optional


class EdgeMaintenanceWorker:
    """周期执行单轮幂等业务维护，单轮异常只影响当前一轮。"""

    def __init__(
        self,
        run_round: Callable[[int], Mapping[str, Any]],
        *,
        interval_seconds: float = 0.5,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.run_round = run_round
        self.interval_seconds = interval_seconds
        self.clock_ns = clock_ns
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._round_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_success_at_ns: Optional[int] = None
        self._last_error_code: Optional[str] = None
        self._last_round_duration_ms = 0.0
        self._last_summary: Mapping[str, Any] | None = None
        self._rounds_total = 0
        self._errors_total = 0
        self._skipped_total = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="edge-maintenance", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float | None = None) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=timeout_seconds or self.interval_seconds + 2.0)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def health(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "maintenance_worker_running": running,
                "last_success_at_ns": self._last_success_at_ns,
                "last_error_code": self._last_error_code,
                "last_round_duration_ms": self._last_round_duration_ms,
                "last_summary": dict(self._last_summary) if self._last_summary else None,
                "rounds_total": self._rounds_total,
                "errors_total": self._errors_total,
                "skipped_rounds_total": self._skipped_total,
            }

    def run_round_once(self) -> bool:
        """同步执行一轮维护；与后台轮次互斥，返回是否实际执行。"""
        if not self._round_lock.acquire(blocking=False):
            with self._lock:
                self._skipped_total += 1
            return False
        try:
            started = self.monotonic()
            try:
                summary = self.run_round(self.clock_ns())
            except Exception as error:
                duration_ms = max((self.monotonic() - started) * 1000.0, 0.0)
                with self._lock:
                    self._errors_total += 1
                    self._last_error_code = getattr(error, "code", type(error).__name__)
                    self._last_round_duration_ms = duration_ms
                self.logger.warning("业务维护轮次失败: %s", error)
                return True
            duration_ms = max((self.monotonic() - started) * 1000.0, 0.0)
            with self._lock:
                self._rounds_total += 1
                self._last_success_at_ns = self.clock_ns()
                self._last_error_code = None
                self._last_round_duration_ms = duration_ms
                self._last_summary = summary
            return True
        finally:
            self._round_lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self.monotonic()
            self.run_round_once()
            elapsed = max(self.monotonic() - started, 0.0)
            self._stop.wait(max(self.interval_seconds - elapsed, 0.0))
