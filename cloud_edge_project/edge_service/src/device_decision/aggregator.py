"""Compatibility facade for V1.2 device-round aggregation."""

from __future__ import annotations

from compatibility.bearing_v12.decision_mapper import aggregate_bearing_round
from core.consistency_engine import ConsistencyPolicy
from core.diagnosis_contracts import (
    BearingDecisionResult,
    DeviceDecisionResult,
    RoundClosureReason,
)


def aggregate_device_round(
    bearing_results: tuple[BearingDecisionResult, ...],
    *,
    expected_bearing_ids: tuple[str, ...],
    closure_reason: RoundClosureReason,
    closed_at_ns: int,
    consistency_policy: ConsistencyPolicy | None = None,
) -> DeviceDecisionResult:
    """Keep the legacy signature while delegating policy through the core engine."""
    return aggregate_bearing_round(
        bearing_results,
        expected_bearing_ids=expected_bearing_ids,
        closure_reason=closure_reason,
        closed_at_ns=closed_at_ns,
        policy=consistency_policy,
    )
