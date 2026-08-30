from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    RoundClosureReason,
)
from core.bearing_actions import ACTION_TO_STATE, action_for_grade
from device_decision import (
    DeviceDecisionRevisionService,
    DeviceDecisionRoundRepository,
    aggregate_device_round,
)
from result_lifecycle import BearingResultRepository


def _bearing(
    bearing_id: str, *, grade: int, confidence: float, quality: float,
    state: BearingLifecycleStatus = BearingLifecycleStatus.FINAL_EDGE,
) -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id=f"bearing_round_01_{bearing_id}_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}", decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}", lifecycle_state=state,
        bearing_state="normal", confidence=confidence, data_quality_score=quality,
        risk_level="low", action_grade=grade,
        recommended_action=("continue_operation", "enhanced_monitoring", "scheduled_inspection", "urgent_intervention", "shutdown")[grade],
        decision_source="EDGE", review_status="NOT_REQUIRED", degraded=state is BearingLifecycleStatus.PROVISIONAL,
        edge_result_id=f"edge_{bearing_id}", cloud_result_id=None, model_version="edge_model_v1",
        created_at_ns=1, edge_accepted_at_ns=2,
    )


def _legacy_aggregate(
    bearing_results: tuple[BearingDecisionResult, ...],
    *,
    expected_bearing_ids: tuple[str, ...],
    closure_reason: RoundClosureReason,
    closed_at_ns: int,
) -> DeviceDecisionResult:
    """Frozen Stage-5 oracle used to prove the new policy is field-equivalent."""
    by_bearing = {item.bearing_id: item for item in bearing_results}
    ordered = tuple(by_bearing[item] for item in expected_bearing_ids if item in by_bearing)
    missing = tuple(item for item in expected_bearing_ids if item not in by_bearing)
    if closure_reason is RoundClosureReason.ROUND_TIMEOUT:
        status = DeviceDecisionStatus.INCOMPLETE
    elif any(item.lifecycle_state is BearingLifecycleStatus.PROVISIONAL for item in ordered):
        status = DeviceDecisionStatus.PROVISIONAL
    else:
        status = DeviceDecisionStatus.FINAL
    final_grade = max(item.action_grade for item in ordered)
    action = action_for_grade(final_grade)
    grades = [item.action_grade for item in ordered]
    conflict = len(grades) > 1 and max(grades) - min(grades) >= 2
    shared = lambda field: next(iter({getattr(item, field) for item in ordered}))
    return DeviceDecisionResult(
        result_id="pending",
        revision=1,
        replaces_result_id=None,
        device_id=shared("device_id"),
        task_id=shared("task_id"),
        decision_round_id=shared("decision_round_id"),
        expected_bearing_ids=expected_bearing_ids,
        received_bearing_ids=tuple(item.bearing_id for item in ordered),
        missing_bearing_ids=missing,
        bearing_result_ids=tuple(item.result_id for item in ordered),
        status=status,
        closure_reason=closure_reason,
        final_state=ACTION_TO_STATE[action],
        final_action_grade=final_grade,
        final_action=action,
        confidence=min(item.confidence for item in ordered),
        data_quality_score=min(item.data_quality_score for item in ordered),
        has_conflict=conflict,
        conflict_reasons=("DEVICE_ACTION_GRADE_CONFLICT",) if conflict else (),
        decision_source="EDGE",
        degraded=status is not DeviceDecisionStatus.FINAL,
        affects_realtime_action=True,
        arbitration_id=None,
        closed_at_ns=closed_at_ns,
        created_at_ns=closed_at_ns,
    )


@pytest.mark.parametrize(
    ("bearings", "expected", "closure_reason"),
    [
        (
            (
                _bearing("bearing_a", grade=0, confidence=.95, quality=1.0),
                _bearing("bearing_b", grade=2, confidence=.80, quality=.90),
            ),
            ("bearing_a", "bearing_b"),
            RoundClosureReason.ALL_BEARINGS_FINAL,
        ),
        (
            (
                _bearing("bearing_a", grade=1, confidence=.90, quality=.90),
                _bearing(
                    "bearing_b",
                    grade=1,
                    confidence=.80,
                    quality=.80,
                    state=BearingLifecycleStatus.PROVISIONAL,
                ),
            ),
            ("bearing_a", "bearing_b"),
            RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL,
        ),
        (
            (_bearing("bearing_a", grade=3, confidence=.75, quality=.65),),
            ("bearing_a", "bearing_b"),
            RoundClosureReason.ROUND_TIMEOUT,
        ),
    ],
)
def test_stage6_consistency_policy_matches_frozen_v12_oracle(
    bearings,
    expected,
    closure_reason,
) -> None:
    assert aggregate_device_round(
        bearings,
        expected_bearing_ids=expected,
        closure_reason=closure_reason,
        closed_at_ns=10,
    ) == _legacy_aggregate(
        bearings,
        expected_bearing_ids=expected,
        closure_reason=closure_reason,
        closed_at_ns=10,
    )


def test_aggregator_uses_v12_max_action_min_confidence_quality_and_conflict_span() -> None:
    result = aggregate_device_round(
        (_bearing("bearing_a", grade=0, confidence=.95, quality=1.0), _bearing("bearing_b", grade=2, confidence=.80, quality=.90)),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=10,
    )

    assert result.status == "FINAL"
    assert result.final_action_grade == 2
    assert result.final_action == "scheduled_inspection"
    assert result.confidence == .80
    assert result.data_quality_score == .90
    assert result.has_conflict is True
    assert result.conflict_reasons == ("DEVICE_ACTION_GRADE_CONFLICT",)


def test_aggregator_marks_provisional_round_degraded() -> None:
    result = aggregate_device_round(
        (_bearing("bearing_a", grade=1, confidence=.9, quality=.9), _bearing("bearing_b", grade=1, confidence=.8, quality=.8, state=BearingLifecycleStatus.PROVISIONAL)),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL,
        closed_at_ns=10,
    )

    assert result.status == "PROVISIONAL"
    assert result.degraded is True
    assert result.affects_realtime_action is True


def test_round_repository_grants_close_authority_once_via_versioned_cas(tmp_path) -> None:
    repository = DeviceDecisionRoundRepository(tmp_path / "edge-results.db")
    created = repository.register_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_bearing_ids=("bearing_a", "bearing_b"), opened_at_ns=1,
    )

    assert repository.close_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_version=created["version"], closure_reason=RoundClosureReason.ROUND_TIMEOUT,
        closed_at_ns=10,
    ) is True
    assert repository.close_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_version=created["version"], closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=11,
    ) is False
    closed = repository.get_round("machine_01", "task_001", "round_01")
    assert closed is not None
    assert closed["state"] == "CLOSED"
    assert closed["closure_reason"] == "ROUND_TIMEOUT"


def test_round_close_and_initial_result_commit_atomically_once(tmp_path) -> None:
    repository = DeviceDecisionRoundRepository(tmp_path / "edge-results.db")
    opened = repository.register_round(
        device_id="machine_01",
        task_id="task_001",
        decision_round_id="round_01",
        expected_bearing_ids=("bearing_a", "bearing_b"),
        opened_at_ns=1,
    )
    draft = aggregate_device_round(
        (
            _bearing("bearing_a", grade=0, confidence=.9, quality=.9),
            _bearing("bearing_b", grade=2, confidence=.8, quality=.8),
        ),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_FINAL,
        closed_at_ns=10,
    )
    barrier = threading.Barrier(2)

    def close_once():
        barrier.wait()
        return repository.close_round_and_save_initial_result(
            draft, expected_version=opened["version"]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: close_once(), range(2)))

    saved = [result for result in results if result is not None]
    assert len(saved) == 1
    assert saved[0].revision == 1
    closed = repository.get_round("machine_01", "task_001", "round_01")
    assert closed is not None
    assert closed["current_device_result_id"] == saved[0].result_id
    assert repository.get_current_result("machine_01", "task_001", "round_01") == saved[0]


def test_closed_provisional_round_can_create_historical_correction_but_timeout_cannot(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-results.db")
    round_repository = DeviceDecisionRoundRepository(tmp_path / "edge-results.db")
    first = bearing_repository.save_revision(_bearing("bearing_a", grade=1, confidence=.9, quality=.9))
    second = bearing_repository.save_revision(
        _bearing("bearing_b", grade=1, confidence=.8, quality=.8, state=BearingLifecycleStatus.PROVISIONAL)
    )
    opened = round_repository.register_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_bearing_ids=("bearing_a", "bearing_b"), opened_at_ns=1,
    )
    assert round_repository.close_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_version=opened["version"], closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL,
        closed_at_ns=10,
    )
    initial = round_repository.save_revision(aggregate_device_round(
        (first, second), expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL, closed_at_ns=10,
    ))

    correction = DeviceDecisionRevisionService(round_repository, bearing_repository).correct_closed_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01", now_ns=20,
    )

    assert correction is not None
    expected_correction = replace(
        _legacy_aggregate(
            (first, second),
            expected_bearing_ids=("bearing_a", "bearing_b"),
            closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL,
            closed_at_ns=20,
        ),
        result_id="device_round_01_r2",
        revision=2,
        replaces_result_id=initial.result_id,
        status=DeviceDecisionStatus.CORRECTED,
        decision_source="HISTORICAL_CORRECTION",
        affects_realtime_action=False,
    )
    assert correction == expected_correction

    timeout_round = round_repository.register_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_timeout",
        expected_bearing_ids=("bearing_a",), opened_at_ns=1,
    )
    assert round_repository.close_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_timeout",
        expected_version=timeout_round["version"], closure_reason=RoundClosureReason.ROUND_TIMEOUT,
        closed_at_ns=10,
    )
    assert DeviceDecisionRevisionService(round_repository, bearing_repository).correct_closed_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_timeout", now_ns=20,
    ) is None
