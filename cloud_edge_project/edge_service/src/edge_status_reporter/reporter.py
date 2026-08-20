# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

from .collectors import AcceleratorDetector, ResourceCollector
from .contracts import BusinessStatusSnapshot, EdgeStatusReport, NetworkSnapshot
from .transport import HttpStatusTarget, SendOutcome


class BusinessStatusSource(Protocol):
    def snapshot(self) -> BusinessStatusSnapshot: ...


class NetworkCollector(Protocol):
    def collect(self) -> NetworkSnapshot: ...


class DeliveryTracker:
    """EDGE-1: 单个目标的上报交付跟踪。

    - 维护连续失败/最近成功/最近失败等字段；
    - 失败 WARNING 按 1/3/10/每 30 次节流，健康运行时零噪音；
    - 恢复后输出一条 INFO（含前次失败数与持续时长）；
    - record() / snapshot() 通过同一把锁保证线程安全。
    """

    # 第 1、3、10 次必打，之后每 30 次再打一次，避免每秒×重试刷屏。
    _WARN_STEPS = (1, 3, 10)

    def __init__(
        self,
        *,
        name: str,
        clock_ns: Callable[[], int],
        monotonic: Callable[[], float],
        logger: logging.Logger | None = None,
    ) -> None:
        self.name = name
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self.consecutive_failures = 0
        # EDGE-4：该 target 上累计“最终交付失败”的条目数（内部 retry 已耗尽）。
        # 恢复成功后清零 consecutive_failures，但累计历史保留不归零；
        # 服务重启后按当前 health 生命周期归零，不持久化。
        self.dropped_report_count = 0
        self.first_failure_ns: int | None = None
        self.first_failure_at_monotonic: float | None = None
        self.last_failure_ns: int | None = None
        self.last_success_ns: int | None = None
        self.last_error: str | None = None
        # EDGE-2：最近一次“最终交付失败”的错误原因。成功恢复后保留，
        # 直到下一次失败覆盖；与 last_error 的“当前故障链”语义分离，
        # 保证恢复后仍能查询最近一次失败原因。
        self.last_failure_error: str | None = None

    def record(self, outcome: SendOutcome) -> None:
        now = self._monotonic()
        wall = self._clock_ns()
        with self._lock:
            if outcome.success:
                if self.consecutive_failures > 0:
                    self.logger.info(
                        "edge status delivery recovered: target=%s "
                        "previous_failures=%d failed_duration_seconds=%.1f",
                        self.name,
                        self.consecutive_failures,
                        now - self.first_failure_at_monotonic
                        if self.first_failure_at_monotonic is not None
                        else 0.0,
                    )
                    self.first_failure_ns = None
                    self.first_failure_at_monotonic = None
                self.consecutive_failures = 0
                self.last_success_ns = wall
                self.last_error = None
                return
            # 失败
            if self.consecutive_failures == 0:
                self.first_failure_ns = wall
                self.first_failure_at_monotonic = now
            self.consecutive_failures += 1
            # EDGE-4：SendOutcome 已是 Transport 内部重试后的最终结果，
            # success=False 即最终交付失败 → 累计一次。
            self.dropped_report_count += 1
            self.last_failure_ns = wall
            self.last_error = (outcome.error or "UNKNOWN")[:256]
            # EDGE-2：同一次最终失败里同步记录历史失败原因（恢复后不清）。
            self.last_failure_error = self.last_error
            failures = self.consecutive_failures
            if failures in self._WARN_STEPS or failures % 30 == 0:
                self.logger.warning(
                    "edge status delivery failed: target=%s "
                    "consecutive_failures=%d error=%s",
                    self.name,
                    failures,
                    self.last_error,
                )

    def status(self) -> str:
        with self._lock:
            if self.last_success_ns is None:
                # 从未成功交付过：一旦出现过失败即为 FAILED。
                return "FAILED" if self.consecutive_failures > 0 else "OK"
            if self.consecutive_failures > 0:
                return "DEGRADED"
            return "OK"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "status": self.status_locked(),
                "consecutive_failures": self.consecutive_failures,
                "dropped_report_count": self.dropped_report_count,
                "first_failure_ns": self.first_failure_ns,
                "last_failure_ns": self.last_failure_ns,
                "last_success_ns": self.last_success_ns,
                "last_error": self.last_error,
                "last_failure_error": self.last_failure_error,
            }

    def status_locked(self) -> str:
        if self.last_success_ns is None:
            return "FAILED" if self.consecutive_failures > 0 else "OK"
        if self.consecutive_failures > 0:
            return "DEGRADED"
        return "OK"


class EdgeStatusReporter:
    def __init__(
        self,
        *,
        status_source: BusinessStatusSource,
        resource_collector: ResourceCollector,
        accelerator_detector: AcceleratorDetector,
        network_collector: NetworkCollector,
        targets: tuple[HttpStatusTarget, ...],
        interval_seconds: float,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.status_source = status_source
        self.resource_collector = resource_collector
        self.accelerator_detector = accelerator_detector
        self.network_collector = network_collector
        self.targets = targets
        self.interval_seconds = interval_seconds
        self.clock_ns = clock_ns
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # EDGE-1: 每个目标独立跟踪交付状态（Scheduler / Cloud 各自计数）。
        self._trackers: dict[str, DeliveryTracker] = {
            target.name: DeliveryTracker(
                name=target.name,
                clock_ns=clock_ns,
                monotonic=monotonic,
                logger=self.logger,
            )
            for target in targets
        }

    def report_once(self) -> EdgeStatusReport | None:
        try:
            business = self.status_source.snapshot()
            resources = self.resource_collector.collect()
            accelerators = self.accelerator_detector.detect()
            network = self.network_collector.collect()
            report = EdgeStatusReport(
                edge_node_id=business.edge_node_id,
                reported_at_ns=self.clock_ns(),
                resources=resources,
                accelerators=accelerators,
                network_to_scheduler=network,
                queue_length=business.queue_length,
                models=business.models,
                last_task_activity_ns=business.last_task_activity_ns,
                queue_measurement_status=business.queue_measurement_status,
                queue_breakdown=business.queue_breakdown,
            )
        except Exception:
            self.logger.exception("状态快照采集失败，本轮跳过")
            return None
        payload = report.as_dict()
        for target in self.targets:
            tracker = self._trackers.get(target.name)
            try:
                outcome = target.send(payload)
            except Exception as exc:
                # 兜底：即使 Transport/FakeTarget 抛出编程异常，
                # 也不让单个目标中断整个上报循环，且计为一次失败。
                outcome = SendOutcome(
                    success=False,
                    status_code=None,
                    error=type(exc).__name__,
                    attempts=0,
                )
            if tracker is not None:
                tracker.record(outcome)
        return report

    def health(self) -> dict[str, Any]:
        """EDGE-1: 暴露 Reporter 交付健康状态（供 /health 接线，非逐字段读内部）。

        通过一次 snapshot() 取一致快照，避免类似 NET-4 的半更新数据。
        """
        target_snapshots = [tracker.snapshot() for tracker in self._trackers.values()]
        target_snapshots.sort(key=lambda item: item["name"])
        rank = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
        worst = "OK"
        for target in target_snapshots:
            if rank[target["status"]] > rank[worst]:
                worst = target["status"]
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "status": worst.lower(),
            "targets": target_snapshots,
        }

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
