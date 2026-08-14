from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    RoundClosureReason,
)
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
    round_repository.save_revision(aggregate_device_round(
        (first, second), expected_bearing_ids=("bearing_a", "bearing_b"),
        closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL, closed_at_ns=10,
    ))

    correction = DeviceDecisionRevisionService(round_repository, bearing_repository).correct_closed_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01", now_ns=20,
    )

    assert correction is not None
    assert correction.status == "CORRECTED"
    assert correction.affects_realtime_action is False
    assert correction.revision == 2

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
