"""Map the V1.2 bearing decision API onto the generic consistency engine."""

from __future__ import annotations

from core.consistency_engine import (
    ConsistencyEngine,
    ConsistencyPolicy,
    ConsistencyRequest,
    ConsistencyUnit,
)
from compatibility.bearing_v12.diagnosis_contracts import (
    BearingDecisionResult,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    RoundClosureReason,
)
from scenarios.bearing.decision import BearingConsistencyPolicy


def aggregate_bearing_round(
    bearing_results: tuple[BearingDecisionResult, ...],
    *,
    expected_bearing_ids: tuple[str, ...],
    closure_reason: RoundClosureReason,
    closed_at_ns: int,
    policy: ConsistencyPolicy | None = None,
) -> DeviceDecisionResult:
    request = ConsistencyRequest(
        units=tuple(
            ConsistencyUnit(
                unit_id=item.bearing_id,
                lifecycle_status=item.lifecycle_state.value,
                confidence=item.confidence,
                data_quality_score=item.data_quality_score,
                action_level=item.action_grade,
            )
            for item in bearing_results
        ),
        expected_unit_ids=expected_bearing_ids,
        closure_reason=closure_reason.value,
        closed_at_ns=closed_at_ns,
    )
    decision = ConsistencyEngine(policy or BearingConsistencyPolicy()).evaluate(request)
    by_bearing = {item.bearing_id: item for item in bearing_results}
    ordered = tuple(by_bearing[item] for item in expected_bearing_ids if item in by_bearing)
    return DeviceDecisionResult(
        result_id="pending",
        revision=1,
        replaces_result_id=None,
        device_id=_shared_field(ordered, "device_id"),
        task_id=_shared_field(ordered, "task_id"),
        decision_round_id=_shared_field(ordered, "decision_round_id"),
        expected_bearing_ids=expected_bearing_ids,
        received_bearing_ids=decision.received_unit_ids,
        missing_bearing_ids=decision.missing_unit_ids,
        bearing_result_ids=tuple(item.result_id for item in ordered),
        status=DeviceDecisionStatus(decision.status),
        closure_reason=closure_reason,
        final_state=decision.final_state,
        final_action_grade=decision.final_action_level,
        final_action=decision.final_action,
        confidence=decision.confidence,
        data_quality_score=decision.data_quality_score,
        has_conflict=decision.has_conflict,
        conflict_reasons=decision.conflict_reasons,
        decision_source=decision.decision_source,
        degraded=decision.degraded,
        affects_realtime_action=decision.affects_realtime_action,
        arbitration_id=None,
        closed_at_ns=closed_at_ns,
        created_at_ns=closed_at_ns,
    )


def _shared_field(results: tuple[BearingDecisionResult, ...], field: str) -> str:
    if not results:
        raise ValueError("an incomplete timeout result requires at least one bearing result")
    values = {getattr(item, field) for item in results}
    if len(values) != 1:
        raise ValueError(f"bearing results do not share {field}")
    return values.pop()
