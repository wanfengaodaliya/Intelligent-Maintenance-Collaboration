from __future__ import annotations

from core.diagnosis_contracts import CloudBearingResult, EdgeBearingResult, PacketRoute
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository


def _edge_result() -> EdgeBearingResult:
    return EdgeBearingResult(
        result_id="edge_dw_01_model_v1",
        device_id="machine_01",
        task_id="task_001",
        bearing_id="bearing_02",
        sender_id="sender_02",
        decision_round_id="round_01",
        diagnosis_window_id="dw_01",
        window_start_sequence=1,
        window_end_sequence=1,
        window_start_ns=0,
        window_end_ns=50_000_000,
        contributing_packet_ids=("packet_001",),
        bearing_state="normal",
        confidence=0.95,
        data_quality_score=1.0,
        risk_level="low",
        action_grade=0,
        recommended_action="continue_operation",
        model_version="edge_model_v1",
        created_at_ns=50_000_001,
    )


def _decision(route: PacketRoute) -> dict:
    return {
        "decision_id": "decision_01",
        "device_id": "machine_01",
        "task_id": "task_001",
        "bearing_id": "bearing_02",
        "decision_round_id": "round_01",
        "diagnosis_window_id": "dw_01",
        "route": route.value,
        "result_instruction": {
            "result_status": {
                PacketRoute.EDGE: "FINAL",
                PacketRoute.CLOUD_NOW: "WAITING_CLOUD",
                PacketRoute.DEFER: "PROVISIONAL",
            }[route],
            "review_status": "NOT_REQUIRED" if route is PacketRoute.EDGE else "PENDING_CLOUD",
            "degraded": route is PacketRoute.DEFER,
        },
    }


def test_route_lifecycle_matrix_persists_single_current_revision(tmp_path) -> None:
    repository = BearingResultRepository(tmp_path / "edge-results.db")
    manager = BearingResultLifecycleManager(repository)

    edge = manager.apply_route(_edge_result(), _decision(PacketRoute.EDGE), accepted_at_ns=60)
    waiting = manager.apply_route(_edge_result(), _decision(PacketRoute.CLOUD_NOW), accepted_at_ns=61)
    provisional = manager.apply_route(_edge_result(), _decision(PacketRoute.DEFER), accepted_at_ns=62)

    assert edge.lifecycle_state == "FINAL_EDGE"
    assert edge.decision_source == "FINAL_EDGE"
    assert waiting.lifecycle_state == "WAITING_CLOUD"
    assert waiting.degraded is False
    assert provisional.lifecycle_state == "PROVISIONAL"
    assert provisional.degraded is True
    assert repository.get_current("machine_01", "task_001", "round_01", "bearing_02") == provisional


def test_route_identity_mismatch_is_rejected(tmp_path) -> None:
    manager = BearingResultLifecycleManager(BearingResultRepository(tmp_path / "edge-results.db"))
    decision = _decision(PacketRoute.EDGE)
    decision["decision_round_id"] = "round_other"

    try:
        manager.apply_route(_edge_result(), decision, accepted_at_ns=60)
    except ValueError as error:
        assert "identity" in str(error)
    else:
        raise AssertionError("route identity mismatch must be rejected")


def test_cloud_result_replaces_waiting_edge_result_after_full_identity_validation(tmp_path) -> None:
    repository = BearingResultRepository(tmp_path / "edge-results.db")
    manager = BearingResultLifecycleManager(repository)
    waiting = manager.apply_route(
        _edge_result(), _decision(PacketRoute.CLOUD_NOW), accepted_at_ns=60
    )
    cloud = CloudBearingResult(
        result_id="cloud_dw_01_v2",
        review_id="review_01",
        device_id="machine_01",
        task_id="task_001",
        bearing_id="bearing_02",
        sender_id="sender_02",
        decision_round_id="round_01",
        diagnosis_window_id="dw_01",
        window_start_sequence=1,
        window_end_sequence=1,
        window_start_ns=0,
        window_end_ns=50_000_000,
        bearing_state="warning",
        confidence=0.90,
        data_quality_score=0.99,
        risk_level="medium",
        action_grade=2,
        recommended_action="scheduled_inspection",
        model_version="cloud_model_v1",
        created_at_ns=70,
    )

    final = manager.apply_cloud_result(cloud, accepted_at_ns=71)

    assert final.lifecycle_state == "FINAL_CLOUD"
    assert final.decision_source == "CLOUD"
    assert final.review_status == "REVIEWED"
    assert final.cloud_result_id == cloud.result_id
    assert final.replaces_result_id == waiting.result_id
    assert final.revision == 2


def test_cloud_result_with_mismatched_window_is_rejected(tmp_path) -> None:
    repository = BearingResultRepository(tmp_path / "edge-results.db")
    manager = BearingResultLifecycleManager(repository)
    manager.apply_route(_edge_result(), _decision(PacketRoute.CLOUD_NOW), accepted_at_ns=60)
    cloud = CloudBearingResult(
        result_id="cloud_dw_other_v2", review_id="review_01", device_id="machine_01",
        task_id="task_001", bearing_id="bearing_02", sender_id="sender_02",
        decision_round_id="round_01", diagnosis_window_id="dw_other",
        window_start_sequence=1, window_end_sequence=1, window_start_ns=0,
        window_end_ns=50_000_000, bearing_state="warning", confidence=0.90,
        data_quality_score=0.99, risk_level="medium", action_grade=2,
        recommended_action="scheduled_inspection", model_version="cloud_model_v1", created_at_ns=70,
    )

    try:
        manager.apply_cloud_result(cloud, accepted_at_ns=71)
    except ValueError as error:
        assert "identity" in str(error)
    else:
        raise AssertionError("cloud result with mismatched identity must be rejected")
