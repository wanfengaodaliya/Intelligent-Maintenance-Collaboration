"""Stable contracts for packet, bearing-window, bearing-task and device results.

This is the real implementation. core/bearing_workflow_contracts.py is a compatibility shim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from scenarios.bearing._compat.bearing_actions import action_for_grade


PACKETS_PER_WINDOW = 20
WINDOWS_PER_BEARING = 4
PACKETS_PER_BEARING = PACKETS_PER_WINDOW * WINDOWS_PER_BEARING

FINAL_EDGE = "FINAL_EDGE"
FINAL_CLOUD = "FINAL_CLOUD"
REVIEW_NOT_REQUIRED = "NOT_REQUIRED"
REVIEW_PENDING = "PENDING"
REVIEW_QUEUED = "QUEUED"
REVIEW_SUCCEEDED = "SUCCEEDED"

REVIEW_PACKET = "PACKET"
REVIEW_BEARING_WINDOW = "BEARING_WINDOW"
REVIEW_DEVICE = "DEVICE"

JOB_PENDING = "PENDING"
JOB_WAITING_RAW = "WAITING_RAW"
JOB_RUNNING = "RUNNING"
JOB_SUCCEEDED = "SUCCEEDED"
JOB_FAILED = "FAILED"
JOB_STATUSES = {
    JOB_PENDING,
    JOB_WAITING_RAW,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_FAILED,
}


@dataclass(frozen=True)
class FinalPacketResult:
    result_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    packet_id: str
    sequence_number: int
    action_grade: int
    confidence: float
    data_quality_score: float
    risk_level: str
    decision_source: str
    raw_data_ref: str
    valid: bool = True

    def __post_init__(self) -> None:
        _identity_fields(self)
        if not 1 <= self.sequence_number <= PACKETS_PER_BEARING:
            raise ValueError("sequence_number must be in [1, 80]")
        _grade(self.action_grade)
        _score(self.confidence, "confidence")
        _score(self.data_quality_score, "data_quality_score")
        if self.decision_source not in {FINAL_EDGE, FINAL_CLOUD}:
            raise ValueError("decision_source must be FINAL_EDGE or FINAL_CLOUD")

    @property
    def recommended_action(self) -> str:
        return action_for_grade(self.action_grade)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recommended_action": self.recommended_action}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FinalPacketResult":
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class BearingWindowResult:
    result_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    window_index: int
    sequence_start: int
    sequence_end: int
    packet_count: int
    valid_packet_count: int
    action_grade: int
    confidence: float
    data_quality_score: float
    result_source: str
    review_status: str
    review_required: bool
    review_reasons: tuple[str, ...] = ()
    packet_result_ids: tuple[str, ...] = ()
    raw_data_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identity_fields(self)
        if not 1 <= self.window_index <= WINDOWS_PER_BEARING:
            raise ValueError("window_index must be in [1, 4]")
        expected_start = (self.window_index - 1) * PACKETS_PER_WINDOW + 1
        if self.sequence_start != expected_start or self.sequence_end != expected_start + 19:
            raise ValueError("window sequence range does not match window_index")
        if not 0 <= self.valid_packet_count <= self.packet_count <= PACKETS_PER_WINDOW:
            raise ValueError("invalid packet counts")
        _grade(self.action_grade)
        _score(self.confidence, "confidence")
        _score(self.data_quality_score, "data_quality_score")

    @property
    def recommended_action(self) -> str:
        return action_for_grade(self.action_grade)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recommended_action": self.recommended_action}


@dataclass(frozen=True)
class BearingTaskResult:
    result_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    window_results: tuple[BearingWindowResult, ...]
    latest_action_grade: int
    max_action_grade: int
    persistent_action_grade: int
    recommended_action_grade: int
    confidence: float
    data_quality_score: float
    trend: str
    rule_facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identity_fields(self)
        if len(self.window_results) != WINDOWS_PER_BEARING:
            raise ValueError("BearingTaskResult requires exactly four windows")
        if tuple(item.window_index for item in self.window_results) != (1, 2, 3, 4):
            raise ValueError("bearing windows must be ordered 1 to 4")
        for grade in (
            self.latest_action_grade,
            self.max_action_grade,
            self.persistent_action_grade,
            self.recommended_action_grade,
        ):
            _grade(grade)
        _score(self.confidence, "confidence")
        _score(self.data_quality_score, "data_quality_score")

    @property
    def recommended_action(self) -> str:
        return action_for_grade(self.recommended_action_grade)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["window_results"] = [item.as_dict() for item in self.window_results]
        result["recommended_action"] = self.recommended_action
        return result


@dataclass(frozen=True)
class DeviceTaskResult:
    result_id: str
    device_id: str
    task_id: str
    expected_bearing_ids: tuple[str, ...]
    bearing_results: tuple[BearingTaskResult, ...]
    status: str
    action_grade: Optional[int]
    conflict: bool
    conflict_reasons: tuple[str, ...] = ()
    decision_source: Optional[str] = None
    final_report: Optional[dict[str, Any]] = field(default=None, compare=False)

    @property
    def recommended_action(self) -> Optional[str]:
        return action_for_grade(self.action_grade) if self.action_grade is not None else None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["bearing_results"] = [item.as_dict() for item in self.bearing_results]
        result["recommended_action"] = self.recommended_action
        return result


def _identity_fields(value: object) -> None:
    for name in ("result_id", "device_id", "task_id", "bearing_id", "sender_id"):
        item = getattr(value, name, None)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("%s must be a non-empty string" % name)


def _grade(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
        raise ValueError("action grade must be an integer from 0 to 4")


def _score(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ValueError("%s must be in [0, 1]" % name)