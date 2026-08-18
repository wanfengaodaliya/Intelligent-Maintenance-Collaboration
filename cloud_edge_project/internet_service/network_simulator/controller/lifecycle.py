"""Monotonic scheduling primitives used by the application lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from threading import Event
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class LoopSummary:
    ticks_completed: int
    overrun_count: int
    stop_reason: str


class MonotonicSimulationLoop:
    def __init__(
        self,
        service: Any,
        *,
        update_interval_seconds: float,
        duration_seconds: float,
        shutdown_event: Event,
        logger: Any,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if update_interval_seconds <= 0:
            raise ValueError("update_interval_seconds must be greater than zero")
        if duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        self._service = service
        self._interval = update_interval_seconds
        self._duration = duration_seconds
        self._shutdown_event = shutdown_event
        self._logger = logger
        self._monotonic = monotonic
        self._wait = shutdown_event.wait if wait is None else wait
        self._now_ns = now_ns

    def run(self) -> LoopSummary:
        started_at = self._monotonic()
        end_at = None if self._duration == 0 else started_at + self._duration
        next_deadline = started_at + self._interval
        ticks_completed = 0
        overrun_count = 0

        while True:
            if self._shutdown_event.is_set():
                reason = "shutdown"
                break
            now = self._monotonic()
            if end_at is not None and now >= end_at:
                reason = "duration"
                break
            target = next_deadline if end_at is None else min(next_deadline, end_at)
            if self._wait(max(0.0, target - now)):
                reason = "shutdown"
                break
            now = self._monotonic()
            if end_at is not None and now >= end_at:
                reason = "duration"
                break

            tick = self._service.next_tick
            self._service.run_tick(tick, timestamp_ns=self._now_ns())
            ticks_completed += 1
            next_deadline += self._interval
            completed_at = self._monotonic()
            if completed_at > next_deadline:
                overrun_count += 1
                missed_deadlines = (
                    math.floor((completed_at - next_deadline) / self._interval) + 1
                )
                next_deadline += missed_deadlines * self._interval
                self._logger.warning(
                    "Tick %s overran its next deadline by %.6f seconds",
                    tick,
                    completed_at - (next_deadline - missed_deadlines * self._interval),
                )

        return LoopSummary(ticks_completed, overrun_count, reason)
