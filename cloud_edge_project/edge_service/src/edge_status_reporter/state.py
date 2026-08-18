# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

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
        # 运行时状态源：在运行时装配完成后可注入，替代固定值快照。
        self._queue_length_provider: Optional[Callable[[], int]] = None
        self._models_provider: Optional[Callable[[], tuple[ModelStatus, ...]]] = None
        self._activity_ns_provider: Optional[Callable[[], int]] = None

    def attach_runtime_providers(
        self,
        *,
        queue_length_provider: Optional[Callable[[], int]] = None,
        models_provider: Optional[Callable[[], tuple[ModelStatus, ...]]] = None,
        activity_ns_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        """注入真实运行时的队列、模型与活动状态源。"""
        with self._lock:
            if queue_length_provider is not None:
                self._queue_length_provider = queue_length_provider
            if models_provider is not None:
                self._models_provider = models_provider
            if activity_ns_provider is not None:
                self._activity_ns_provider = activity_ns_provider

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
            queue_provider = self._queue_length_provider
            models_provider = self._models_provider
            activity_provider = self._activity_ns_provider
        # 状态是时效数据：提供方异常时回退到安全默认值，绝不影响业务主链路。
        try:
            extra_activity = int(activity_provider()) if activity_provider is not None else 0
        except Exception:
            extra_activity = 0
        last_activity = max(last_activity, max(extra_activity, 0))
        try:
            queue_length = max(int(queue_provider()), 0) if queue_provider is not None else 0
        except Exception:
            queue_length = 0
        models = None
        if models_provider is not None:
            try:
                candidate = models_provider()
                if candidate:
                    models = tuple(candidate)
            except Exception:
                models = None
        if models is None:
            models = (ModelStatus(self.model_version, load_status),)
        return BusinessStatusSnapshot(
            edge_node_id=self.edge_node_id,
            queue_length=queue_length,
            models=models,
            last_task_activity_ns=last_activity,
        )
