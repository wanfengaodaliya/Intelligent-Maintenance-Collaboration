"""Thread-safe runtime observations used by cloud node status reporting."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class CloudRuntimeSnapshot:
    queue_length: int
    last_task_activity_ns: int


class CloudRuntimeState:
    """Track inference work accepted by this cloud service process."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.time_ns) -> None:
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._inflight_inferences = 0
        self._last_task_activity_ns = 0

    def begin_inference(self) -> None:
        self._record_activity(increment=1)

    def finish_inference(self) -> None:
        self._record_activity(increment=-1)

    def snapshot(self) -> CloudRuntimeSnapshot:
        with self._lock:
            return CloudRuntimeSnapshot(
                queue_length=self._inflight_inferences,
                last_task_activity_ns=self._last_task_activity_ns,
            )

    def _record_activity(self, *, increment: int) -> None:
        now_ns = int(self._clock_ns())
        with self._lock:
            self._inflight_inferences = max(self._inflight_inferences + increment, 0)
            self._last_task_activity_ns = max(self._last_task_activity_ns, now_ns)
