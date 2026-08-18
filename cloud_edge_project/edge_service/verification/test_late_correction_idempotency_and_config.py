from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    CloudBearingResult,
    EdgeBearingResult,
    PacketRoute,
    RoundClosureReason,
)
from device_decision import (
    DeviceDecisionRevisionService,
    DeviceDecisionRoundRepository,
    aggregate_device_round,
)
from edge_runtime.config import EdgeRuntimeConfig
from edge_runtime.v12_flow import V12DecisionFlow
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository


def _edge(bearing_id: str, grade: int) -> EdgeBearingResult:
    return EdgeBearingResult(
        result_id=f"edge_dw_{bearing_id}_v1",
        device_id="machine_01",
        task_id="task_001",
        bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}",
        decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}",
        window_start_sequence=1,
        window_end_sequence=1,
        window_start_ns=0,
        window_end_ns=50_000_000,
        contributing_packet_ids=(f"packet_{bearing_id}",),
        bearing_state="normal" if grade == 0 else "warning",
        confidence=0.9,
        data_quality_score=0.9,
        risk_level="low" if grade == 0 else "high",
        action_grade=grade,
        recommended_action=(
            "continue_operation" if grade == 0 else "urgent_intervention"
        ),
        model_version="edge_model_v1",
        created_at_ns=10,
    )


def _route(result: EdgeBearingResult) -> dict:
    return {
        "device_id": result.device_id,
        "task_id": result.task_id,
        "bearing_id": result.bearing_id,
        "decision_round_id": result.decision_round_id,
        "diagnosis_window_id": result.diagnosis_window_id,
        "route": PacketRoute.EDGE.value,
        "result_instruction": {
            "result_status": "FINAL",
            "review_status": "NOT_REQUIRED",
            "degraded": False,
        },
    }


def _conflict_flow(tmp_path, *, published: list, reviews: list | None = None, retention_ns: int | None = None):
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(tmp_path / "edge.db")),
        DeviceDecisionRoundRepository(tmp_path / "edge.db"),
        late_correction_retention_ns=retention_ns,
        on_device_result=published.append,
        on_manual_review=None if reviews is None else reviews.append,
    )
    flow.apply_edge_result(
        _edge("bearing_a", 0), _route(_edge("bearing_a", 0)),
        expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11,
    )
    _, initial = flow.apply_edge_result(
        _edge("bearing_b", 3), _route(_edge("bearing_b", 3)),
        expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12,
    )
    assert initial is not None and initial.has_conflict
    return flow, initial


def _arbitration_payload(initial) -> dict:
    return {
        "device_id": "machine_01",
        "task_id": "task_001",
        "decision_round_id": "round_01",
        "device_result_revision": initial.revision,
        "arbitration_id": "arb_001",
        "final_action": "urgent_intervention",
        "confidence": 0.9,
    }


def test_duplicate_arbitration_callback_produces_single_revision(tmp_path) -> None:
    published: list = []
    flow, initial = _conflict_flow(tmp_path, published=published)
    payload = _arbitration_payload(initial)

    first = flow.apply_cloud_arbitration_result(payload, accepted_at_ns=40)
    second = flow.apply_cloud_arbitration_result(payload, accepted_at_ns=41)

    assert first is not None and second is not None
    assert first.result_id == second.result_id
    assert first.revision == 2 and second.revision == 2
    # 重复回调不再发布：初始结果一次 + 仲裁修正一次。
    assert len(published) == 2
    assert flow.device_rounds.get_arbitration_receipt("arb_001") is not None


def test_arbitration_beyond_retention_goes_to_manual_review(tmp_path) -> None:
    published: list = []
    reviews: list = []
    flow, initial = _conflict_flow(
        tmp_path, published=published, reviews=reviews, retention_ns=10
    )
    payload = _arbitration_payload(initial)

    with pytest.raises(ValueError):
        # 轮次关闭于 12，保留窗口只有 10，迟到 11 必须转人工核查。
        flow.apply_cloud_arbitration_result(payload, accepted_at_ns=23)

    assert reviews and reviews[0]["error_code"] == "LATE_CORRECTION_BEYOND_RETENTION"
    current = flow.device_rounds.get_current_result("machine_01", "task_001", "round_01")
    assert current is not None and current.revision == initial.revision
    assert len(published) == 1  # 只有初始冲突结果被发布过


def _bearing(bearing_id: str, *, grade: int, state=BearingLifecycleStatus.FINAL_EDGE) -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id=f"bearing_round_01_{bearing_id}_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id=bearing_id,
        sender_id=f"sender_{bearing_id}", decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}", lifecycle_state=state,
        bearing_state="normal", confidence=0.9, data_quality_score=0.9,
        risk_level="low", action_grade=grade,
        recommended_action=("continue_operation", "enhanced_monitoring", "scheduled_inspection", "urgent_intervention", "shutdown")[grade],
        decision_source="EDGE", review_status="NOT_REQUIRED", degraded=state is BearingLifecycleStatus.PROVISIONAL,
        edge_result_id=f"edge_{bearing_id}", cloud_result_id=None, model_version="edge_model_v1",
        created_at_ns=1, edge_accepted_at_ns=2,
    )


def test_repeated_correction_with_same_conclusion_is_skipped(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge.db")
    round_repository = DeviceDecisionRoundRepository(tmp_path / "edge.db")
    bearing_repository.save_revision(_bearing("bearing_a", grade=1))
    bearing_repository.save_revision(
        _bearing("bearing_b", grade=1, state=BearingLifecycleStatus.PROVISIONAL)
    )
    opened = round_repository.register_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_bearing_ids=("bearing_a", "bearing_b"), opened_at_ns=1,
    )
    assert round_repository.close_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01",
        expected_version=opened["version"],
        closure_reason=RoundClosureReason.ALL_BEARINGS_WITH_PROVISIONAL,
        closed_at_ns=10,
    )
    service = DeviceDecisionRevisionService(round_repository, bearing_repository)

    first = service.correct_closed_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01", now_ns=20,
    )
    second = service.correct_closed_round(
        device_id="machine_01", task_id="task_001", decision_round_id="round_01", now_ns=30,
    )

    assert first is not None
    assert second is None  # 结论未变化的重复修正不产生新版本


def test_late_cloud_result_matching_conclusion_is_marked_confirmed(tmp_path) -> None:
    repository = BearingResultRepository(tmp_path / "edge.db")
    manager = BearingResultLifecycleManager(repository)
    edge = EdgeBearingResult(
        result_id="edge_dw_01_model_v1", device_id="machine_01", task_id="task_001",
        bearing_id="bearing_02", sender_id="sender_02", decision_round_id="round_01",
        diagnosis_window_id="dw_01", window_start_sequence=1, window_end_sequence=1,
        window_start_ns=0, window_end_ns=50_000_000, contributing_packet_ids=("packet_001",),
        bearing_state="normal", confidence=0.95, data_quality_score=1.0, risk_level="low",
        action_grade=0, recommended_action="continue_operation",
        model_version="edge_model_v1", created_at_ns=50_000_001,
    )
    manager.apply_route(
        edge,
        {
            "decision_id": "decision_01", "device_id": "machine_01", "task_id": "task_001",
            "bearing_id": "bearing_02", "decision_round_id": "round_01",
            "diagnosis_window_id": "dw_01", "route": PacketRoute.DEFER.value,
            "result_instruction": {"result_status": "PROVISIONAL", "review_status": "PENDING_CLOUD", "degraded": True},
        },
        accepted_at_ns=60,
    )
    cloud = CloudBearingResult(
        result_id="cloud_dw_01_v2", review_id="review_01", device_id="machine_01",
        task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
        decision_round_id="round_01", diagnosis_window_id="dw_01",
        window_start_sequence=1, window_end_sequence=1, window_start_ns=0,
        window_end_ns=50_000_000, bearing_state="normal", confidence=0.97,
        data_quality_score=1.0, risk_level="low", action_grade=0,
        recommended_action="continue_operation", model_version="cloud_model_v1", created_at_ns=70,
    )

    late = manager.apply_cloud_result(cloud, accepted_at_ns=71, round_open=False)

    assert late.lifecycle_state is BearingLifecycleStatus.LATE_CLOUD_CONFIRMED


def test_default_runtime_config_is_valid() -> None:
    assert EdgeRuntimeConfig().validate() == []


def test_timeout_relationships_are_rejected_at_validation() -> None:
    base = EdgeRuntimeConfig()

    connect_over_read = replace(
        base, v12=replace(base.v12, http_connect_timeout_ms=2_500, http_read_timeout_ms=1_000)
    )
    assert any("connect" in error for error in connect_over_read.validate())

    budget_over_cloud_now = replace(
        base, v12=replace(base.v12, http_connect_timeout_ms=1_500, http_read_timeout_ms=2_000)
    )
    assert any("cloud_now_timeout_ms" in error for error in budget_over_cloud_now.validate())

    round_too_small = replace(base, v12=replace(base.v12, round_timeout_ms=3_000))
    assert any("round_timeout_ms" in error for error in round_too_small.validate())

    bad_maintenance = replace(base, maintenance=replace(base.maintenance, interval_seconds=20.0))
    assert any("maintenance" in error for error in bad_maintenance.validate())


def _load_validator_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_multi_edge_config.py"
    spec = importlib.util.spec_from_file_location("validate_multi_edge_config", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multi_edge_compose_configuration_is_valid() -> None:
    module = _load_validator_module()
    assert module.validate() == []


def test_multi_edge_validator_detects_duplicate_topics(tmp_path) -> None:
    module = _load_validator_module()
    compose_path = Path(__file__).resolve().parents[1] / "compose.multi-edge.yml"
    broken = compose_path.read_text(encoding="utf-8").replace(
        "EDGE_MQTT_INPUT_TOPIC: edge/edge_002/input",
        "EDGE_MQTT_INPUT_TOPIC: edge/edge_001/input",
    )
    target = tmp_path / "compose.broken.yml"
    target.write_text(broken, encoding="utf-8")

    errors = module.validate(target)

    assert any("input topic" in error for error in errors)
