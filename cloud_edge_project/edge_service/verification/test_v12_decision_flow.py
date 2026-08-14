from __future__ import annotations

from core.diagnosis_contracts import CloudBearingResult, EdgeBearingResult, PacketRoute
from device_decision import DeviceDecisionRoundRepository
from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_runtime.coordinator import _edge_bearing_result
from edge_runtime.config import EdgeRuntimeConfig, V12RuntimeConfig
from edge_runtime.v12_flow import V12DecisionFlow
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository


def _edge(bearing_id: str, *, grade: int) -> EdgeBearingResult:
    return EdgeBearingResult(
        result_id=f"edge_dw_{bearing_id}_v1", device_id="machine_01", task_id="task_001",
        bearing_id=bearing_id, sender_id=f"sender_{bearing_id}", decision_round_id="round_01",
        diagnosis_window_id=f"dw_{bearing_id}", window_start_sequence=1, window_end_sequence=1,
        window_start_ns=0, window_end_ns=50_000_000, contributing_packet_ids=(f"packet_{bearing_id}",),
        bearing_state="normal", confidence=.9, data_quality_score=.9, risk_level="low",
        action_grade=grade, recommended_action=("continue_operation", "enhanced_monitoring", "scheduled_inspection", "urgent_intervention", "shutdown")[grade],
        model_version="edge_model_v1", created_at_ns=10,
    )


def _route(result: EdgeBearingResult, route: PacketRoute) -> dict:
    return {
        "device_id": result.device_id, "task_id": result.task_id, "bearing_id": result.bearing_id,
        "decision_round_id": result.decision_round_id, "diagnosis_window_id": result.diagnosis_window_id,
        "route": route.value,
        "result_instruction": {
            "result_status": {PacketRoute.EDGE: "FINAL", PacketRoute.CLOUD_NOW: "WAITING_CLOUD", PacketRoute.DEFER: "PROVISIONAL"}[route],
            "review_status": "NOT_REQUIRED" if route is PacketRoute.EDGE else "PENDING_CLOUD",
            "degraded": route is PacketRoute.DEFER,
        },
    }


def test_runtime_flow_persists_bearing_results_and_closes_complete_round(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
    )
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=1)

    assert flow.apply_edge_result(
        first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11,
    )[1] is None
    _, device = flow.apply_edge_result(
        second, _route(second, PacketRoute.DEFER), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12,
    )

    assert device is not None
    assert device.status == "PROVISIONAL"
    assert device.closure_reason == "ALL_BEARINGS_WITH_PROVISIONAL"
    assert flow.device_rounds.get_round("machine_01", "task_001", "round_01")["state"] == "CLOSED"


def test_coordinator_converts_completed_packet_to_v12_edge_bearing_result() -> None:
    completion = PacketExecutionCompleted(
        request_id="request_01", device_id="machine_01", task_id="task_001",
        bearing_id="bearing_a", sender_id="sender_a", packet_id="packet_001",
        sequence_number=1, status="SUCCEEDED", error_code=None, started_at_ns=1,
        finished_at_ns=2, edge=EdgeResult("fault", .9, "high", "edge_model_v1"),
        data_quality_score=.8,
    )

    result = _edge_bearing_result(completion, {"end_generate_timestamp_ns": 50_000_000})

    assert result.result_id.startswith("edge_dw_")
    assert result.decision_round_id.startswith("round_")
    assert result.bearing_state == "abnormal"
    assert result.action_grade == 4
    assert result.recommended_action == "shutdown"


def test_cloud_result_corrects_closed_provisional_round_without_reopening_it(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
    )
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=1)
    flow.apply_edge_result(first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11)
    flow.apply_edge_result(second, _route(second, PacketRoute.DEFER), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12)
    cloud = CloudBearingResult(
        result_id="cloud_dw_bearing_b_v1", review_id="review_01", device_id="machine_01",
        task_id="task_001", bearing_id="bearing_b", sender_id="sender_bearing_b", decision_round_id="round_01",
        diagnosis_window_id="dw_bearing_b", window_start_sequence=1, window_end_sequence=1,
        window_start_ns=0, window_end_ns=50_000_000, bearing_state="warning", confidence=.95,
        data_quality_score=.95, risk_level="medium", action_grade=2,
        recommended_action="scheduled_inspection", model_version="cloud_model_v1", created_at_ns=20,
    )

    _, correction = flow.apply_cloud_result(cloud, accepted_at_ns=21)

    assert correction is not None
    assert correction.status == "CORRECTED"
    assert correction.affects_realtime_action is False
    assert flow.device_rounds.get_round("machine_01", "task_001", "round_01")["state"] == "CLOSED"


def test_timeout_scan_closes_open_round_once_as_incomplete(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
    )
    first = _edge("bearing_a", grade=0)
    flow.apply_edge_result(
        first, _route(first, PacketRoute.CLOUD_NOW), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=10,
    )

    closed = flow.finalize_timeouts(now_ns=111, round_timeout_ns=100)

    assert len(closed) == 1
    assert closed[0].status == "INCOMPLETE"
    assert closed[0].closure_reason == "ROUND_TIMEOUT"
    assert flow.finalize_timeouts(now_ns=112, round_timeout_ns=100) == ()


def test_cloud_now_timeout_becomes_provisional_before_round_timeout(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
    )
    first = _edge("bearing_a", grade=0)
    flow.apply_edge_result(
        first, _route(first, PacketRoute.CLOUD_NOW), expected_bearing_ids=("bearing_a",), accepted_at_ns=10,
    )

    closed = flow.promote_cloud_now_timeouts(now_ns=110, cloud_now_timeout_ns=100)

    assert len(closed) == 1
    assert closed[0].status == "PROVISIONAL"
    assert bearing_repository.get_current("machine_01", "task_001", "round_01", "bearing_a").lifecycle_state == "PROVISIONAL"


def test_runtime_config_rejects_round_timeout_shorter_than_cloud_now_deadline() -> None:
    errors = EdgeRuntimeConfig(v12=V12RuntimeConfig(
        cloud_now_timeout_ms=3000, round_finalize_grace_ms=500, round_timeout_ms=3499
    )).validate()

    assert "v12.round_timeout_ms must cover cloud-now timeout plus finalize grace" in errors
