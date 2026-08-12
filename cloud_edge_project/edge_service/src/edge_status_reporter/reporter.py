# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

from .collectors import AcceleratorDetector, ResourceCollector
from .contracts import BusinessStatusSnapshot, EdgeStatusReport
from .transport import HttpStatusTarget


class BusinessStatusSource(Protocol):
    def snapshot(self) -> BusinessStatusSnapshot: ...


class EdgeStatusReporter:
    def __init__(
        self,
        *,
        status_source: BusinessStatusSource,
        resource_collector: ResourceCollector,
        accelerator_detector: AcceleratorDetector,
        targets: tuple[HttpStatusTarget, ...],
        interval_seconds: float,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.status_source = status_source
        self.resource_collector = resource_collector
        self.accelerator_detector = accelerator_detector
        self.targets = targets
        self.interval_seconds = interval_seconds
        self.clock_ns = clock_ns
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def report_once(self) -> EdgeStatusReport | None:
        try:
            business = self.status_source.snapshot()
            resources = self.resource_collector.collect()
            accelerators = self.accelerator_detector.detect()
            report = EdgeStatusReport(
                edge_node_id=business.edge_node_id,
                reported_at_ns=self.clock_ns(),
                resources=resources,
                accelerators=accelerators,
                queue_length=business.queue_length,
                models=business.models,
                last_task_activity_ns=business.last_task_activity_ns,
            )
        except Exception:
            self.logger.exception("状态快照采集失败，本轮跳过")
            return None
        payload = report.as_dict()
        for target in self.targets:
            try:
                target.send(payload)
            except Exception:
                self.logger.exception("状态目标异常 target=%s", target.name)
        return report

    def start(self) -> None:
        with self._lock:
            if self._stopping:
                return
            if self._thread is not None:
                if self._thread.is_alive():
                    return
                self._thread = None
            try:
                self.resource_collector.warm_up()
            except Exception as exc:
                self.logger.warning("状态资源采集预热失败: %s", type(exc).__name__)
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="edge-status-reporter",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._stop.set()
                raise
        self.logger.info("Edge Status Reporter 已启动 interval=%.3fs", self.interval_seconds)

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stopping = True
            self._stop.set()
        thread.join(timeout=self.stop_timeout_seconds)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
            self._stopping = False
        if thread.is_alive():
            self.logger.warning("Edge Status Reporter 未在停止等待时间内退出")
        else:
            self.logger.info("Edge Status Reporter 已停止")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def stop_timeout_seconds(self) -> float:
        send_budget = sum(
            float(getattr(target, "timeout_seconds", 0.0))
            * (int(getattr(target, "retry_count", -1)) + 1)
            for target in self.targets
        )
        return max(self.interval_seconds + 1.0, send_budget + 1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            started_at = self.monotonic()
            try:
                self.report_once()
            except Exception:
                self.logger.exception("Edge Status Reporter 循环异常")
            elapsed = max(self.monotonic() - started_at, 0.0)
            self._stop.wait(max(self.interval_seconds - elapsed, 0.0))
