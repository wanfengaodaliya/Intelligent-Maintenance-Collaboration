"""Immutable cross-plugin events used by the V3 controller."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TickStarted:
    tick: int
    timestamp_ns: int


@dataclass(frozen=True, slots=True)
class LinkStateGenerated:
    tick: int
    link_id: str


@dataclass(frozen=True, slots=True)
class LinkStateApplied:
    tick: int
    link_id: str
    success: bool


@dataclass(frozen=True, slots=True)
class LinkScoreCalculated:
    tick: int
    link_id: str
    score: float


@dataclass(frozen=True, slots=True)
class RejectedLink:
    link_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReportCompleted:
    report_sequence: int
    link_count: int
    success: bool
    status_code: int | None
    retry_count: int
    duration_ms: float
    error: str | None
    accepted_count: int | None = None
    rejected_count: int | None = None
    rejected_links: tuple[RejectedLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportDropped:
    report_sequence: int
    reason: str = "queue_full_drop_oldest"
