"""Non-blocking Reporter plugin with a dedicated delivery worker."""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Lock, Thread, current_thread
import logging
import time

from domain.events import ReportCompleted, ReportDropped
from domain.models import RuntimeSnapshot
from plugins.base import BasePlugin, PluginContext

from .client import ReportOutcome, ReportSendResult, SchedulerReportClient
from .queue import DropOldestReportQueue
from .report_builder import ReportBuilder
from .schemas import NetworkReport


ClientFactory = Callable[[PluginContext], SchedulerReportClient]

_LOGGER = logging.getLogger("network_simulator.plugins.reporter")


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
        # NET-3：一次报告经 Transport 内部所有重试后仍最终未成功交付 → +1。
        # 内部某次 retry 失败但最终发送成功不得 +1（final delivery 才计数）。
        # 语义：累计历史，服务重启后按当前 health 生命周期归零，不持久化。
        self._dropped_report_count = 0
        self._consecutive_failures = 0
        # NET-1: 部分成功（部分链路被接受 + 部分被拒绝）的累计数。
        # 不计入 dropped_report_count，也不计入 total-delivery consecutive_failures。
        self._partial_failure_count = 0
        self._last_success_ns: int | None = None
        self._last_failure_ns: int | None = None
        self._failure_streak_started_ns: int | None = None
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
            # ENV-2: 初始化成功打印一次权威报告目标，明确区分 standalone stub 与真实 Scheduler，
            # 避免运维误把 compose 里 healthy 的 stub 当作真实接收方。
            _LOGGER.info(
                "network reporter configured: target=%s",
                context.config.reporter.scheduler_url,
            )

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
            last_result = self._last_result
            last_success = None if last_result is None else last_result.success
            last_outcome = None if last_result is None else last_result.outcome
            if self._stopped:
                status = "stopped"
            elif self._stopping:
                status = "stopping"
            elif self._started:
                # NET-1：部分成功是可观测异常，仍降级但不计入整份 dropped。
                if last_outcome in (
                    ReportOutcome.TOTAL_FAILURE,
                    ReportOutcome.PARTIAL_SUCCESS,
                ):
                    status = "degraded"
                elif last_success is False:
                    status = "degraded"
                else:
                    status = "ok"
            elif self._context is not None:
                status = "initialized"
            else:
                status = "created"
            return {
                "status": status,
                "queue_size": 0 if self._queue is None else len(self._queue),
                "dropped_count": self._dropped_count,
                "dropped_report_count": self._dropped_report_count,
                "partial_failure_count": self._partial_failure_count,
                "last_report_success": last_success,
                "last_outcome": last_outcome,
                "last_error": None if last_result is None else last_result.error,
                "last_accepted_count": (
                    None if last_result is None else last_result.accepted_count
                ),
                "last_rejected_count": (
                    None if last_result is None else last_result.rejected_count
                ),
                "last_rejected_links": (
                    []
                    if last_result is None
                    else [
                        {"link_id": item.link_id, "reason": item.reason}
                        for item in last_result.rejected_links
                    ]
                ),
                "last_success_ns": self._last_success_ns,
                "last_failure_ns": self._last_failure_ns,
                "consecutive_failures": self._consecutive_failures,
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
                    now_ns = self._now_ns()
                    outcome = result.outcome
                    if outcome is ReportOutcome.PARTIAL_SUCCESS:
                        # NET-1：delivery transport 本身成功，只是部分链路被拒绝。
                        # 不计数整份报告 dropped，也不计入 total-delivery 连续失败；
                        # 但按整次成功刷新连续失败链（Scheduler 可达），
                        # 由单独 partial_failure_count + rejected 日志承载可观测性。
                        recovered = self._consecutive_failures > 0
                        previous_failures = self._consecutive_failures
                        streak_started_ns = self._failure_streak_started_ns
                        self._consecutive_failures = 0
                        self._failure_streak_started_ns = None
                        self._last_success_ns = now_ns
                        self._partial_failure_count += 1
                        partial_count = self._partial_failure_count
                        should_log_failure = False
                        failure_count = 0
                    elif outcome is ReportOutcome.FULL_SUCCESS:
                        recovered = self._consecutive_failures > 0
                        previous_failures = self._consecutive_failures
                        streak_started_ns = self._failure_streak_started_ns
                        self._consecutive_failures = 0
                        self._failure_streak_started_ns = None
                        self._last_success_ns = now_ns
                        should_log_failure = False
                        failure_count = 0
                        partial_count = None
                    else:
                        recovered = False
                        previous_failures = 0
                        streak_started_ns = None
                        self._consecutive_failures += 1
                        # NET-3：这是该报告的最终交付失败（client 已耗尽内部重试）。
                        self._dropped_report_count += 1
                        self._last_failure_ns = now_ns
                        if self._failure_streak_started_ns is None:
                            self._failure_streak_started_ns = now_ns
                        failure_count = self._consecutive_failures
                        partial_count = None
                        # AUD-13: rate-limited failure logging. Log the first
                        # failure, then 3rd, 10th and every 30th afterwards so
                        # a down Scheduler stays visible without log spam.
                        should_log_failure = (
                            failure_count in (1, 3, 10) or failure_count % 30 == 0
                        )
                if should_log_failure:
                    _LOGGER.warning(
                        "Scheduler report failed: consecutive_failures=%d "
                        "report_sequence=%d error=%s",
                        failure_count,
                        result.report_sequence,
                        result.error,
                    )
                if partial_count is not None and (
                    partial_count in (1, 3, 10) or partial_count % 30 == 0
                ):
                    _LOGGER.warning(
                        "Scheduler report partially accepted: "
                        "partial_failure_count=%d report_sequence=%d "
                        "accepted_count=%s rejected=%s",
                        partial_count,
                        result.report_sequence,
                        result.accepted_count,
                        result.rejected_count,
                    )
                if recovered:
                    duration_seconds = 0.0
                    if streak_started_ns is not None:
                        duration_seconds = max(
                            0.0, (now_ns - streak_started_ns) / 1_000_000_000
                        )
                    _LOGGER.info(
                        "Scheduler reporter recovered: previous_failures=%d "
                        "failed_duration_seconds=%.1f",
                        previous_failures,
                        duration_seconds,
                    )
                if result.rejected_links:
                    summary = ", ".join(
                        f"{item.link_id}({item.reason})"
                        for item in result.rejected_links
                    )
                    _LOGGER.warning(
                        "Scheduler rejected %d link(s) in report %d: %s",
                        len(result.rejected_links),
                        result.report_sequence,
                        summary,
                    )
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
                            accepted_count=result.accepted_count,
                            rejected_count=result.rejected_count,
                            rejected_links=result.rejected_links,
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
