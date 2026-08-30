"""Backward-compatible exports for the legacy V1.2 diagnosis contracts."""

from compatibility.bearing_v12.diagnosis_contracts import (
    ActionGrade,
    BearingDecisionResult,
    BearingLifecycleStatus,
    CloudBearingResult,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    EdgeBearingResult,
    PacketRoute,
    RoundClosureReason,
    RoundState,
)

__all__ = [
    "ActionGrade",
    "BearingDecisionResult",
    "BearingLifecycleStatus",
    "CloudBearingResult",
    "DeviceDecisionResult",
    "DeviceDecisionStatus",
    "EdgeBearingResult",
    "PacketRoute",
    "RoundClosureReason",
    "RoundState",
]
