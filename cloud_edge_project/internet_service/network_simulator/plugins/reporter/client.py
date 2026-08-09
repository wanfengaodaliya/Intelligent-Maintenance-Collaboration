"""Retrying HTTP client for Scheduler report delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
import time

import requests

from .schemas import NetworkReport, ReportAcknowledgement


@dataclass(frozen=True, slots=True)
class ReportSendResult:
    report_sequence: int
    success: bool
    status_code: int | None
    retry_count: int
    duration_ms: float
    error: str | None


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
    ) -> ReportSendResult:
        return ReportSendResult(
            report_sequence=report.report_sequence,
            success=success,
            status_code=status_code,
            retry_count=retry_count,
            duration_ms=max(0.0, (self._monotonic() - started) * 1000),
            error=error,
        )
