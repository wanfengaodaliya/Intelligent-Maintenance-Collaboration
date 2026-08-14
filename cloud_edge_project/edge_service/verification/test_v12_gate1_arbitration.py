from __future__ import annotations

from types import SimpleNamespace

import cloud_service.app as cloud_api
from cloud_service.config import CloudSettings
from core.diagnosis_contracts import EdgeBearingResult, PacketRoute
from device_decision import DeviceDecisionRoundRepository
from edge_runtime.v12_flow import V12DecisionFlow
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository
from scheduler.deferred_device_dispatcher import DeferredDeviceArbitrationDispatcher
from scheduler.deferred_device_repository import DeferredDeviceArbitrationRepository
from scheduler.device_router import DeviceArbitrationRouter
from scheduler.device_service import DeviceArbitrationService


class _ReadyRegistry:
    def snapshot(self, *_args, **_kwargs):
        return SimpleNamespace(
            is_fresh=True,
            health_status="ONLINE",
            queue_length=0,
            status_message_id="cloud-status-1",
            model_loaded=lambda _model: True,
        )

    def link_snapshot(self, *_args, **_kwargs):
        return SimpleNamespace(
            measurement_status="AVAILABLE",
            connected=True,
            goodput_mbps=20.0,
            rtt_ms_p95=10.0,
            loss_rate=0.0,
            link_id="link-1",
        )


class _CloudClient:
    def dispatch(self, _base_url: str, payload: dict) -> dict:
        result = cloud_api.device_arbitration(payload)
        if not isinstance(result, dict):
            raise AssertionError("cloud arbitration rejected the V1.2 request")
        return result


class _EdgeClient:
    def __init__(self, flow: V12DecisionFlow) -> None:
        self.flow = flow

    def deliver(self, _base_url: str, payload: dict) -> dict:
        result = self.flow.apply_cloud_arbitration_result(payload, accepted_at_ns=40)
        return {"accepted": True, "device_result_id": result.result_id}


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


def test_gate1_device_conflict_round_travels_scheduler_cloud_and_back_to_edge(
    tmp_path, monkeypatch
) -> None:
    edge_database = tmp_path / "edge.db"
    scheduler_database = tmp_path / "scheduler.db"
    routed_requests: list[dict] = []
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(edge_database)),
        DeviceDecisionRoundRepository(edge_database),
        on_device_conflict=routed_requests.append,
    )
    flow.apply_edge_result(
        _edge("bearing_a", 0),
        _route(_edge("bearing_a", 0)),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        accepted_at_ns=11,
    )
    _, initial = flow.apply_edge_result(
        _edge("bearing_b", 3),
        _route(_edge("bearing_b", 3)),
        expected_bearing_ids=("bearing_a", "bearing_b"),
        accepted_at_ns=12,
    )
    assert initial is not None and initial.has_conflict
    service = DeviceArbitrationService(
        DeviceArbitrationRouter(cloud_registry=_ReadyRegistry(), clock_ns=lambda: 30),
        DeferredDeviceArbitrationRepository(scheduler_database),
    )
    decision = service.route(routed_requests[0] | {"edge_node_id": "edge_01"})
    monkeypatch.setattr(
        cloud_api,
        "load_cloud_settings",
        lambda: CloudSettings("mock", "", "", "", 1.0, tmp_path / "cloud.db"),
    )
    dispatched = DeferredDeviceArbitrationDispatcher(
        service.repository,
        cloud_url_lookup=lambda _cloud_id: "http://cloud.example",
        edge_url_lookup=lambda _edge_id: "http://edge.example",
        client=_CloudClient(),
        edge_result_client=_EdgeClient(flow),
        clock_ns=lambda: 30,
    ).dispatch_once(now_ns=30)

    assert decision["decision_round_id"] == "round_01"
    assert dispatched is not None and dispatched["state"] == "SUCCEEDED"
    current = flow.device_rounds.get_current_result(
        "machine_01", "task_001", "round_01"
    )
    assert current is not None
    assert current.revision == 2
    assert current.replaces_result_id == initial.result_id
    assert current.decision_source == "CLOUD_ARBITRATION"
    assert current.arbitration_id == dispatched["arbitration_id"]
