"""V1.2 shared diagnosis enums and immutable identity-bearing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Literal, Mapping


class PacketRoute(str, Enum):
    EDGE = "EDGE"
    CLOUD_NOW = "CLOUD_NOW"
    DEFER = "DEFER"


class BearingLifecycleStatus(str, Enum):
    EDGE_READY = "EDGE_READY"
    WAITING_CLOUD = "WAITING_CLOUD"
    PROVISIONAL = "PROVISIONAL"
    FINAL_EDGE = "FINAL_EDGE"
    FINAL_CLOUD = "FINAL_CLOUD"
    LATE_CLOUD_CONFIRMED = "LATE_CLOUD_CONFIRMED"
    LATE_CLOUD_CORRECTED = "LATE_CLOUD_CORRECTED"


class DeviceDecisionStatus(str, Enum):
    FINAL = "FINAL"
    PROVISIONAL = "PROVISIONAL"
    INCOMPLETE = "INCOMPLETE"
    CORRECTED = "CORRECTED"


class RoundState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RoundClosureReason(str, Enum):
    ALL_BEARINGS_FINAL = "ALL_BEARINGS_FINAL"
    ALL_BEARINGS_WITH_PROVISIONAL = "ALL_BEARINGS_WITH_PROVISIONAL"
    ROUND_TIMEOUT = "ROUND_TIMEOUT"


class ActionGrade(IntEnum):
    CONTINUE_OPERATION = 0
    ENHANCED_MONITORING = 1
    SCHEDULED_INSPECTION = 2
    URGENT_INTERVENTION = 3
    SHUTDOWN = 4


@dataclass(frozen=True)
class EdgeBearingResult:
    """Immutable V1.2 edge model result for one diagnosis window."""

    result_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    decision_round_id: str
    diagnosis_window_id: str
    window_start_sequence: int
    window_end_sequence: int
    window_start_ns: int
    window_end_ns: int
    contributing_packet_ids: tuple[str, ...]
    bearing_state: str
    confidence: float
    data_quality_score: float
    risk_level: str
    action_grade: int
    recommended_action: str
    model_version: str
    created_at_ns: int
    diagnosis_label: str | None = None
    class_probabilities: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        _validate_identity(self)
        _validate_score("confidence", self.confidence)
        _validate_score("data_quality_score", self.data_quality_score)
        if self.action_grade not in range(5):
            raise ValueError("action_grade must be in [0, 4]")
        if not self.contributing_packet_ids:
            raise ValueError("contributing_packet_ids must not be empty")


@dataclass(frozen=True)
class CloudBearingResult:
    """Validated cloud re-review result for the exact diagnosis window."""

    result_id: str
    review_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    decision_round_id: str
    diagnosis_window_id: str
    window_start_sequence: int
    window_end_sequence: int
    window_start_ns: int
    window_end_ns: int
    bearing_state: str
    confidence: float
    data_quality_score: float
    risk_level: str
    action_grade: int
    recommended_action: str
    model_version: str
    created_at_ns: int

    def __post_init__(self) -> None:
        _validate_identity(self)
        if not self.review_id:
            raise ValueError("review_id must not be empty")
        _validate_score("confidence", self.confidence)
        _validate_score("data_quality_score", self.data_quality_score)
        if self.action_grade not in range(5):
            raise ValueError("action_grade must be in [0, 4]")
        if self.window_start_sequence < 1 or self.window_end_sequence < self.window_start_sequence:
            raise ValueError("window sequence range must be positive and ordered")


@dataclass(frozen=True)
class BearingDecisionResult:
    """Persisted V1.2 bearing decision revision owned by the edge node."""

    result_id: str
    revision: int
    replaces_result_id: str | None
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    decision_round_id: str
    diagnosis_window_id: str
    lifecycle_state: BearingLifecycleStatus
    bearing_state: str
    confidence: float
    data_quality_score: float
    risk_level: str
    action_grade: int
    recommended_action: str
    decision_source: str
    review_status: Literal["NOT_REQUIRED", "PENDING_CLOUD", "REVIEWED"]
    degraded: bool
    edge_result_id: str
    cloud_result_id: str | None
    model_version: str
    created_at_ns: int
    edge_accepted_at_ns: int
    diagnosis_label: str | None = None
    class_probabilities: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        _validate_identity(self)
        _validate_score("confidence", self.confidence)
        _validate_score("data_quality_score", self.data_quality_score)
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.action_grade not in range(5):
            raise ValueError("action_grade must be in [0, 4]")


@dataclass(frozen=True)
class DeviceDecisionResult:
    """One closed V1.2 device decision round or a historical correction."""

    result_id: str
    revision: int
    replaces_result_id: str | None
    device_id: str
    task_id: str
    decision_round_id: str
    expected_bearing_ids: tuple[str, ...]
    received_bearing_ids: tuple[str, ...]
    missing_bearing_ids: tuple[str, ...]
    bearing_result_ids: tuple[str, ...]
    status: DeviceDecisionStatus
    closure_reason: RoundClosureReason
    final_state: str
    final_action_grade: int
    final_action: str
    confidence: float
    data_quality_score: float
    has_conflict: bool
    conflict_reasons: tuple[str, ...]
    decision_source: str
    degraded: bool
    affects_realtime_action: bool
    arbitration_id: str | None
    closed_at_ns: int
    created_at_ns: int

    def __post_init__(self) -> None:
        for field in ("result_id", "device_id", "task_id", "decision_round_id"):
            if not getattr(self, field):
                raise ValueError(f"{field} must not be empty")
        if not self.expected_bearing_ids or len(set(self.expected_bearing_ids)) != len(self.expected_bearing_ids):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        if self.revision < 1 or self.final_action_grade not in range(5):
            raise ValueError("invalid device result revision or action grade")
        _validate_score("confidence", self.confidence)
        _validate_score("data_quality_score", self.data_quality_score)

    def as_dict(self) -> dict:
        from dataclasses import asdict

        value = asdict(self)
        value["status"] = self.status.value
        value["closure_reason"] = self.closure_reason.value
        return value


def _validate_identity(result: EdgeBearingResult | BearingDecisionResult) -> None:
    for field in (
        "result_id",
        "device_id",
        "task_id",
        "bearing_id",
        "sender_id",
        "decision_round_id",
        "diagnosis_window_id",
    ):
        if not getattr(result, field):
            raise ValueError(f"{field} must not be empty")


def _validate_score(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
