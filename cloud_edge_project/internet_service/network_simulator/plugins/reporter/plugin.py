"""Non-blocking Reporter plugin with a dedicated delivery worker."""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Lock, Thread, current_thread
import time

from domain.events import ReportCompleted, ReportDropped
from domain.models import RuntimeSnapshot
from plugins.base import BasePlugin, PluginContext

from .client import ReportSendResult, SchedulerReportClient
from .queue import DropOldestReportQueue
from .report_builder import ReportBuilder
from .schemas import NetworkReport


ClientFactory = Callable[[PluginContext], SchedulerReportClient]


class ReporterPlugin(BasePlugin):
    name = "reporter"
    dependencies = ("score",)
    required = False

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        now_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        initial_sequence: int | None = None,
    ) -> None:
        if initial_sequence is not None and initial_sequence < 1:
            raise ValueError("initial_sequence must be at least one")
        self._client_factory = client_factory or self._default_client
        self._now_ns = now_ns
        self._monotonic_ns = monotonic_ns
        self._configured_initial_sequence = initial_sequence
        self._context: PluginContext | None = None
        self._builder: ReportBuilder | None = None
        self._client: SchedulerReportClient | None = None
        self._queue: DropOldestReportQueue | None = None
        self._thread: Thread | None = None
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._started = False
        self._stopping = False
        self._stopped = False
        self._client_closing = False
        self._client_closed = False
        self._shutdown_escalated = False
        self._next_sequence = 1
        self._last_enqueued_monotonic_ns: int | None = None
        self._last_result: ReportSendResult | None = None
        self._dropped_count = 0
        self._shutdown_timeout_seconds = 1.0

    def initialize(self, context: PluginContext) -> None:
        with self._condition:
            if self._context is not None:
                raise RuntimeError("Reporter plugin is already initialized")
            self._context = context
            self._builder = ReportBuilder(context.config)
            self._client = self._client_factory(context)
            self._queue = DropOldestReportQueue(
                context.config.reporter.queue_capacity
            )
            self._next_sequence = (
                max(1, self._now_ns())
                if self._configured_initial_sequence is None
                else self._configured_initial_sequence
            )
            self._shutdown_timeout_seconds = (
                context.config.reporter.connect_timeout_seconds
                + context.config.reporter.read_timeout_seconds
                + 0.5
            )
            self._shutdown_escalated = False
            self._stopped = False

    def start(self) -> None:
        with self._condition:
            if self._context is None or self._queue is None or self._client is None:
                raise RuntimeError("Reporter plugin must be initialized before start")
            if self._started:
                raise RuntimeError("Reporter plugin is already started")
            if self._stopping:
                raise RuntimeError("Reporter plugin is stopping")
            if self._stopped:
                raise RuntimeError("stopped Reporter plugin cannot be restarted")
            thread = Thread(
                target=self._run_worker,
                name="network-reporter",
                daemon=True,
            )
            self._thread = thread
            self._started = True
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._started = False
                raise

    def submit(
        self,
        snapshot: RuntimeSnapshot,
        *,
        now_ns: int | None = None,
    ) -> NetworkReport | None:
        timestamp_ns = self._now_ns() if now_ns is None else now_ns
        throttle_time_ns = self._monotonic_ns()
        dropped_sequence: int | None = None
        with self._condition:
            if not self._started or self._builder is None or self._queue is None:
                raise RuntimeError("Reporter plugin must be started before submit")
            assert self._context is not None
            interval_ns = int(
                self._context.config.reporter.report_interval_seconds
                * 1_000_000_000
            )
            if (
                self._last_enqueued_monotonic_ns is not None
                and throttle_time_ns - self._last_enqueued_monotonic_ns
                < interval_ns
            ):
                return None
            report = self._builder.build(
                snapshot,
                report_sequence=self._next_sequence,
                now_ns=timestamp_ns,
            )
            dropped = self._queue.put(report)
            self._next_sequence += 1
            self._last_enqueued_monotonic_ns = throttle_time_ns
            if dropped is not None:
                self._dropped_count += 1
                dropped_sequence = dropped.report_sequence
            context = self._context
        if dropped_sequence is not None:
            context.event_bus.publish(ReportDropped(dropped_sequence))
        return report

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            if not self._stopping:
                self._started = False
                self._stopping = True
                if self._queue is not None:
                    self._queue.close()
            thread = self._thread
            client = self._client
        if thread is current_thread():
            return
        if thread is not None:
            thread.join(timeout=self._shutdown_timeout_seconds)
            if thread.is_alive():
                discarded: tuple[NetworkReport, ...] = ()
                should_cancel = False
                with self._condition:
                    if not self._shutdown_escalated:
                        self._shutdown_escalated = True
                        should_cancel = True
                        if self._queue is not None:
                            discarded = self._queue.close(discard=True)
                            self._dropped_count += len(discarded)
                    context = self._context
                if should_cancel and client is not None:
                    cancel = getattr(client, "cancel", None)
                    if callable(cancel):
                        try:
                            cancel()
                        except Exception:
                            pass
                if context is not None:
                    for report in discarded:
                        context.event_bus.publish(
                            ReportDropped(
                                report.report_sequence,
                                reason="shutdown_timeout",
                            )
                        )
                thread.join(timeout=self._shutdown_timeout_seconds)
                if thread.is_alive():
                    # A Python thread cannot be terminated safely. Keep the
                    # truthful stopping state and let the worker close the
                    # client once the bounded send eventually returns.
                    return
        self._finalize_stop()

    def health(self) -> dict[str, object]:
        with self._condition:
            last_success = (
                None if self._last_result is None else self._last_result.success
            )
            if self._stopped:
                status = "stopped"
            elif self._stopping:
                status = "stopping"
            elif self._started:
                status = "degraded" if last_success is False else "ok"
            elif self._context is not None:
                status = "initialized"
            else:
                status = "created"
            return {
                "status": status,
                "queue_size": 0 if self._queue is None else len(self._queue),
                "dropped_count": self._dropped_count,
                "last_report_success": last_success,
            }

    def _run_worker(self) -> None:
        assert self._queue is not None
        assert self._client is not None
        try:
            while True:
                report = self._queue.get(timeout=0.1)
                if report is None:
                    if self._queue.closed:
                        return
                    continue
                try:
                    result = self._client.send(report)
                except Exception as exc:
                    result = ReportSendResult(
                        report_sequence=report.report_sequence,
                        success=False,
                        status_code=None,
                        retry_count=0,
                        duration_ms=0.0,
                        error=f"{type(exc).__name__}: Reporter client failed",
                    )
                with self._condition:
                    self._last_result = result
                    context = self._context
                if context is not None:
                    context.event_bus.publish(
                        ReportCompleted(
                            report_sequence=result.report_sequence,
                            link_count=len(report.links),
                            success=result.success,
                            status_code=result.status_code,
                            retry_count=result.retry_count,
                            duration_ms=result.duration_ms,
                            error=result.error,
                        )
                    )
        finally:
            self._finalize_stop()

    def _finalize_stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            if self._client_closing:
                while not self._stopped:
                    self._condition.wait()
                return
            self._client_closing = True
            client = self._client
        if client is not None and not self._client_closed:
            try:
                client.close()
            except Exception:
                pass
        with self._condition:
            self._client_closed = True
            self._client_closing = False
            self._thread = None
            self._stopping = False
            self._stopped = True
            self._condition.notify_all()

    @staticmethod
    def _default_client(context: PluginContext) -> SchedulerReportClient:
        config = context.config.reporter
        token = context.config.report_auth_token
        return SchedulerReportClient(
            config.scheduler_url,
            config.connect_timeout_seconds,
            config.read_timeout_seconds,
            config.retry_count,
            config.backoff_base_seconds,
            bearer_token=None if token is None else token.get_secret_value(),
        )
