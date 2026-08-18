# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Callable

from .contracts import BusinessStatusSnapshot, ModelStatus


class EdgeApplicationState:
    def __init__(
        self,
        *,
        edge_node_id: str,
        model_version: str,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.edge_node_id = edge_node_id
        self.model_version = model_version
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._last_task_activity_ns = 0
        self._load_status = "LOADED"

    def touch_task_activity(self) -> None:
        value = self._clock_ns()
        with self._lock:
            self._last_task_activity_ns = max(self._last_task_activity_ns, value)

    def set_load_status(self, value: str) -> None:
        normalized = ModelStatus(self.model_version, value).load_status
        with self._lock:
            self._load_status = normalized

    def snapshot(self) -> BusinessStatusSnapshot:
        with self._lock:
            last_activity = self._last_task_activity_ns
            load_status = self._load_status
        return BusinessStatusSnapshot(
            edge_node_id=self.edge_node_id,
            queue_length=0,
            models=(ModelStatus(self.model_version, load_status),),
            last_task_activity_ns=last_activity,
        )
