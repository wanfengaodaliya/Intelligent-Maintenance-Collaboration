"""Retrying HTTP client for Scheduler report delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
import time

import requests

from domain.events import RejectedLink

from .schemas import NetworkReport, ReportAcknowledgement


class ReportOutcome(StrEnum):
    """NET-1: 批发交付的分类结果。

    - FULL_SUCCESS：HTTP 成功，Scheduler 支持的所有链路均被接受，无真正 rejected
      （skipped 属设计边界，不计为失败）。
    - PARTIAL_SUCCESS：HTTP 成功且至少一部分有效链路已被接受，但存在真正 rejected。
      它不是“整份报告 dropped”，不占用 consecutive_failures/dropped_report_count。
    - TOTAL_FAILURE：HTTP 无法到达、5xx 最终失败、返回合同非法、
      accepted_count==0 且存在应处理链路等——整份有效交付失败。
    """

    FULL_SUCCESS = "FULL_SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    TOTAL_FAILURE = "TOTAL_FAILURE"


@dataclass(frozen=True, slots=True)
class ReportSendResult:
    report_sequence: int
    success: bool
    status_code: int | None
    retry_count: int
    duration_ms: float
    error: str | None
    accepted_count: int | None = None
    rejected_count: int | None = None
    rejected_links: tuple[RejectedLink, ...] = ()
    # NET-1: 显式分类结果。缺省时从 success 推导，保持既有调用方兼容
    # （success=True → FULL_SUCCESS，success=False → TOTAL_FAILURE）。
    outcome: ReportOutcome | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.outcome is None:
            object.__setattr__(
                self,
                "outcome",
                ReportOutcome.FULL_SUCCESS
                if self.success
                else ReportOutcome.TOTAL_FAILURE,
            )


class SchedulerReportClient:
    def __init__(
        self,
        scheduler_url: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        retry_count: int,
        backoff_base_seconds: float,
        *,
        bearer_token: str | None = None,
        session: requests.Session | None = None,
        wait: Callable[[float], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Reporter timeouts must be greater than zero")
        if retry_count < 0 or backoff_base_seconds < 0:
            raise ValueError("Reporter retry settings cannot be negative")
        self._scheduler_url = scheduler_url
        self._timeout = (connect_timeout_seconds, read_timeout_seconds)
        self._retry_count = retry_count
        self._backoff_base_seconds = backoff_base_seconds
        self._token = bearer_token
        self._session = session or requests.Session()
        self._cancel_event = Event()
        self._wait = wait or self._cancel_event.wait
        self._monotonic = monotonic

    def send(self, report: NetworkReport) -> ReportSendResult:
        started = self._monotonic()
        if self._cancel_event.is_set():
            return self._cancelled_result(report, 0, started)
        status_code: int | None = None
        error = "Reporter request failed"
        for attempt in range(self._retry_count + 1):
            if self._cancel_event.is_set():
                return self._cancelled_result(
                    report,
                    max(0, attempt - 1),
                    started,
                )
            retryable = True
            try:
                response = self._session.post(
                    self._scheduler_url,
                    json=report.model_dump(mode="json"),
                    headers=self._headers(),
                    timeout=self._timeout,
                )
                status_code = response.status_code
            except requests.RequestException as exc:
                status_code = None
                error = f"{type(exc).__name__}: Scheduler request failed"
            else:
                if response.status_code == 200:
                    try:
                        acknowledgement = ReportAcknowledgement.model_validate(
                            response.json()
                        )
                    except (ValueError, TypeError):
                        error = "Invalid Scheduler acknowledgement"
                    else:
                        if (
                            acknowledgement.accepted
                            and acknowledgement.report_sequence
                            == report.report_sequence
                        ):
                            return self._result(
                                report,
                                True,
                                response.status_code,
                                attempt,
                                started,
                                None,
                                accepted_count=acknowledgement.accepted_count,
                                rejected_count=acknowledgement.rejected_count,
                                outcome=ReportOutcome.FULL_SUCCESS,
                            )
                        if not acknowledgement.accepted:
                            # HTTP 200 but semantic rejection (partial or full):
                            # deterministic outcome, retrying the same payload
                            # cannot change it. Report the rejected links so the
                            # plugin can log a warning and degrade its health.
                            rejected_links = tuple(
                                RejectedLink(
                                    link_id=item.link_id or "unknown",
                                    reason=item.reason,
                                )
                                for item in (acknowledgement.results or ())
                            )
                            rejected_count = acknowledgement.rejected_count
                            if rejected_count is None:
                                rejected_count = len(rejected_links)
                            # NET-1：HTTP 成功但存在 rejected——
                            # 至少一部分有效链路被接受则判 PARTIAL_SUCCESS，
                            # 否则没有任何有效链路被接受则判 TOTAL_FAILURE。
                            effective_accepted = acknowledgement.accepted_count or 0
                            if effective_accepted > 0:
                                outcome = ReportOutcome.PARTIAL_SUCCESS
                            else:
                                outcome = ReportOutcome.TOTAL_FAILURE
                            summary = ", ".join(
                                f"{item.link_id}({item.reason})"
                                for item in rejected_links
                            )
                            return self._result(
                                report,
                                False,
                                response.status_code,
                                attempt,
                                started,
                                (
                                    "Scheduler rejected "
                                    f"{rejected_count} link(s)"
                                    + (f": {summary}" if summary else "")
                                ),
                                accepted_count=acknowledgement.accepted_count,
                                rejected_count=rejected_count,
                                rejected_links=rejected_links,
                                outcome=outcome,
                            )
                        error = "Scheduler acknowledgement did not match report"
                else:
                    retryable = response.status_code >= 500
                    error = f"Scheduler returned HTTP {response.status_code}"
            if not retryable or attempt == self._retry_count:
                return self._result(
                    report,
                    False,
                    status_code,
                    attempt,
                    started,
                    error,
                )
            if self._cancel_event.is_set() or self._wait(
                self._backoff_base_seconds * (2**attempt)
            ):
                return self._cancelled_result(report, attempt, started)
        raise AssertionError("unreachable Reporter retry state")

    def cancel(self) -> None:
        self._cancel_event.set()

    def close(self) -> None:
        self._session.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _cancelled_result(
        self,
        report: NetworkReport,
        retry_count: int,
        started: float,
    ) -> ReportSendResult:
        return self._result(
            report,
            False,
            None,
            retry_count,
            started,
            "Reporter delivery cancelled",
        )

    def _result(
        self,
        report: NetworkReport,
        success: bool,
        status_code: int | None,
        retry_count: int,
        started: float,
        error: str | None,
        *,
        accepted_count: int | None = None,
        rejected_count: int | None = None,
        rejected_links: tuple[RejectedLink, ...] = (),
        outcome: ReportOutcome | None = None,
    ) -> ReportSendResult:
        return ReportSendResult(
            report_sequence=report.report_sequence,
            success=success,
            status_code=status_code,
            retry_count=retry_count,
            duration_ms=max(0.0, (self._monotonic() - started) * 1000),
            error=error,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            rejected_links=rejected_links,
            outcome=outcome,
        )
