"""Pure V1.2 aggregation for one device decision round."""

from __future__ import annotations

from core.bearing_actions import ACTION_TO_STATE, action_for_grade
from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    RoundClosureReason,
)


def aggregate_device_round(
    bearing_results: tuple[BearingDecisionResult, ...],
    *,
    expected_bearing_ids: tuple[str, ...],
    closure_reason: RoundClosureReason,
    closed_at_ns: int,
) -> DeviceDecisionResult:
    """Aggregate current bearing revisions without any historical trend rule."""
    if not expected_bearing_ids or len(set(expected_bearing_ids)) != len(expected_bearing_ids):
        raise ValueError("expected_bearing_ids must be non-empty and unique")
    by_bearing = {item.bearing_id: item for item in bearing_results}
    if len(by_bearing) != len(bearing_results) or any(item not in expected_bearing_ids for item in by_bearing):
        raise ValueError("bearing results must be unique expected bearings")
    ordered = tuple(by_bearing[item] for item in expected_bearing_ids if item in by_bearing)
    missing = tuple(item for item in expected_bearing_ids if item not in by_bearing)
    if closure_reason is RoundClosureReason.ROUND_TIMEOUT:
        status = DeviceDecisionStatus.INCOMPLETE
    elif missing:
        raise ValueError("only ROUND_TIMEOUT may close an incomplete round")
    elif any(item.lifecycle_state is BearingLifecycleStatus.PROVISIONAL for item in ordered):
        status = DeviceDecisionStatus.PROVISIONAL
    else:
        status = DeviceDecisionStatus.FINAL
    if not ordered:
        final_grade, confidence, quality = 0, 0.0, 0.0
    else:
        final_grade = max(item.action_grade for item in ordered)
        confidence = min(item.confidence for item in ordered)
        quality = min(item.data_quality_score for item in ordered)
    grades = [item.action_grade for item in ordered]
    conflict = len(grades) > 1 and max(grades) - min(grades) >= 2
    return DeviceDecisionResult(
        result_id="pending",
        revision=1,
        replaces_result_id=None,
        device_id=_shared_field(ordered, "device_id"),
        task_id=_shared_field(ordered, "task_id"),
        decision_round_id=_shared_field(ordered, "decision_round_id"),
        expected_bearing_ids=expected_bearing_ids,
        received_bearing_ids=tuple(item.bearing_id for item in ordered),
        missing_bearing_ids=missing,
        bearing_result_ids=tuple(item.result_id for item in ordered),
        status=status,
        closure_reason=closure_reason,
        final_state=ACTION_TO_STATE[action_for_grade(final_grade)],
        final_action_grade=final_grade,
        final_action=action_for_grade(final_grade),
        confidence=confidence,
        data_quality_score=quality,
        has_conflict=conflict,
        conflict_reasons=("DEVICE_ACTION_GRADE_CONFLICT",) if conflict else (),
        decision_source="EDGE",
        degraded=status is not DeviceDecisionStatus.FINAL,
        affects_realtime_action=True,
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
