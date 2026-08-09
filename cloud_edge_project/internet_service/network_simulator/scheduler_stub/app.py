"""Minimal idempotent Scheduler receiver; it makes no scheduling decisions."""

from __future__ import annotations

from threading import RLock
import time
from typing import Callable

from fastapi import FastAPI, HTTPException

from plugins.reporter.schemas import NetworkReport, ReportAcknowledgement


class ReportSequenceConflictError(ValueError):
    """The same idempotency key was reused for different report content."""


class SchedulerCache:
    def __init__(self) -> None:
        self._lock = RLock()
        self._seen: dict[tuple[str, str, int], NetworkReport] = {}
        self._highest_sequence_by_stream: dict[tuple[str, str], int] = {}
        self._links: dict[str, dict[str, object]] = {}

    def accept(self, report: NetworkReport, received_at_ns: int) -> bool:
        if received_at_ns < 0:
            raise ValueError("received_at_ns cannot be negative")
        key = (
            report.experiment_id,
            report.reporter_id,
            report.report_sequence,
        )
        stream = (report.experiment_id, report.reporter_id)
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None:
                if previous != report:
                    raise ReportSequenceConflictError(
                        "report idempotency key conflicts with existing content"
                    )
                return True
            self._seen[key] = report
            highest_sequence = self._highest_sequence_by_stream.get(stream)
            if (
                highest_sequence is not None
                and report.report_sequence < highest_sequence
            ):
                return False
            self._highest_sequence_by_stream[stream] = report.report_sequence
            for link in report.links:
                existing = self._links.get(link.link_id)
                if existing is not None:
                    same_stream = (
                        existing["experiment_id"] == report.experiment_id
                        and existing["reporter_id"] == report.reporter_id
                    )
                    if same_stream:
                        if int(existing["report_sequence"]) > report.report_sequence:
                            continue
                    elif int(existing["generated_at_ns"]) > report.generated_at_ns:
                        continue
                self._links[link.link_id] = {
                    **link.model_dump(mode="json"),
                    "experiment_id": report.experiment_id,
                    "reporter_id": report.reporter_id,
                    "report_sequence": report.report_sequence,
                    "generated_at_ns": report.generated_at_ns,
                    "received_at_ns": received_at_ns,
                }
            return False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            links = [dict(value) for _, value in sorted(self._links.items())]
            return {"link_count": len(links), "links": links}


def create_app(
    cache: SchedulerCache | None = None,
    *,
    now_ns: Callable[[], int] = time.time_ns,
) -> FastAPI:
    scheduler_cache = cache or SchedulerCache()
    application = FastAPI(title="Network Simulator Fake Scheduler")

    @application.post(
        "/api/v1/network/reports",
        response_model=ReportAcknowledgement,
    )
    def receive_report(report: NetworkReport) -> ReportAcknowledgement:
        try:
            scheduler_cache.accept(report, now_ns())
        except ReportSequenceConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ReportAcknowledgement(
            accepted=True,
            report_sequence=report.report_sequence,
        )

    @application.get("/api/v1/network/cache")
    def get_cache() -> dict[str, object]:
        return scheduler_cache.snapshot()

    @application.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "link_count": scheduler_cache.snapshot()["link_count"]}

    application.state.scheduler_cache = scheduler_cache
    return application


app = create_app()
