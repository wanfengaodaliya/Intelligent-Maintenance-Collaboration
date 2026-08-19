from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from core.diagnosis_contracts import CloudBearingResult, EdgeBearingResult, PacketRoute
from device_decision import DeviceDecisionRoundRepository
from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_runtime.coordinator import _edge_bearing_result
from diagnosis_window import DiagnosisWindowAssembler
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


def test_conflicting_closed_round_emits_v12_scheduler_arbitration_request(tmp_path) -> None:
    requests: list[dict] = []
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
        on_device_conflict=requests.append,
    )
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=3)

    flow.apply_edge_result(
        first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11,
    )
    _, device = flow.apply_edge_result(
        second, _route(second, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12,
    )

    assert device is not None and device.has_conflict
    assert requests == [
        {
            "device_id": "machine_01",
            "task_id": "task_001",
            "decision_round_id": "round_01",
            "device_result_revision": 1,
            "bearing_result_ids": [
                "bearing_round_01_bearing_a_r1",
                "bearing_round_01_bearing_b_r1",
            ],
            "expected_bearing_count": 2,
            "received_bearing_count": 2,
            "bearing_results": [
                {
                    "bearing_id": "bearing_a",
                    "bearing_result_id": "bearing_round_01_bearing_a_r1",
                    "result": "normal",
                    "confidence": 0.9,
                    "risk_level": "low",
                    "action_level": 0,
                    "result_status": "FINAL",
                },
                {
                    "bearing_id": "bearing_b",
                    "bearing_result_id": "bearing_round_01_bearing_b_r1",
                    "result": "normal",
                    "confidence": 0.9,
                    "risk_level": "low",
                    "action_level": 3,
                    "result_status": "FINAL",
                },
            ],
            "comparison": {
                "conflict": True,
                "conflict_type": "ACTION_SPAN",
                "action_level_min": 0,
                "action_level_max": 3,
                "action_level_span": 3,
                "aggregate_confidence": 0.9,
                "low_confidence_bearing_count": 0,
                "provisional_bearing_count": 0,
                "data_complete": True,
            },
            "task_complexity": 0.1,
            "local_arbitration_supported": True,
        }
    ]


def test_cloud_arbitration_creates_a_new_device_revision(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
    )
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=3)
    flow.apply_edge_result(
        first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11,
    )
    _, device = flow.apply_edge_result(
        second, _route(second, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12,
    )

    result = flow.apply_cloud_arbitration_result(
        {
            "arbitration_id": "arbitration_01",
            "device_id": "machine_01",
            "task_id": "task_001",
            "decision_round_id": "round_01",
            "device_result_revision": device.revision,
            "final_action": "scheduled_inspection",
            "confidence": 0.95,
        },
        accepted_at_ns=20,
    )

    assert result is not None
    assert result.revision == 2
    assert result.replaces_result_id == device.result_id
    assert result.decision_source == "CLOUD_ARBITRATION"
    assert result.arbitration_id == "arbitration_01"
    assert result.final_action == "scheduled_inspection"


def test_cloud_arbitration_cannot_reopen_timeout_sealed_round(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
        round_timeout_ns=100,
    )
    first = _edge("bearing_a", grade=0)
    flow.apply_edge_result(
        first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=10,
    )
    timeout = flow.finalize_timeouts(now_ns=111, round_timeout_ns=100)[0]

    result = flow.apply_cloud_arbitration_result(
        {
            "arbitration_id": "arbitration_01",
            "device_id": "machine_01",
            "task_id": "task_001",
            "decision_round_id": "round_01",
            "device_result_revision": timeout.revision,
            "final_action": "scheduled_inspection",
            "confidence": 0.95,
        },
        accepted_at_ns=120,
    )

    assert result is None
    current = flow.device_rounds.get_current_result("machine_01", "task_001", "round_01")
    assert current is not None and current.result_id == timeout.result_id


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
    assert result.bearing_state == "fault"
    assert result.action_grade == 4
    assert result.recommended_action == "shutdown"


def test_coordinator_builds_bearing_result_from_the_complete_diagnosis_window() -> None:
    packets = [
        {
            "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
            "sender_id": "sender_a", "packet_id": f"packet_{sequence:03d}",
            "sequence_number": sequence,
            "start_generate_timestamp_ns": (sequence - 1) * 50_000_000,
            "end_generate_timestamp_ns": sequence * 50_000_000,
            "data": {"vibration": {"sample_rate_hz": 64_000}},
        }
        for sequence in (1, 2)
    ]
    assembler = DiagnosisWindowAssembler(window_ms=100)
    window = next(result for packet in packets for result in assembler.append(packet))
    completion = PacketExecutionCompleted(
        request_id="request_01", device_id="machine_01", task_id="task_001",
        bearing_id="bearing_a", sender_id="sender_a", packet_id="packet_002",
        sequence_number=2, status="SUCCEEDED", error_code=None, started_at_ns=1,
        finished_at_ns=2, edge=EdgeResult("warning", .8, "medium", "edge_model_v1"),
        data_quality_score=.9,
    )

    result = _edge_bearing_result(
        completion, {"end_generate_timestamp_ns": 100_000_000},
        diagnosis_window=window,
    )

    assert result.diagnosis_window_id == window.diagnosis_window_id
    assert result.decision_round_id == window.decision_round_id
    assert result.window_start_sequence == 1
    assert result.window_end_sequence == 2
    assert result.contributing_packet_ids == ("packet_001", "packet_002")


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


def test_closed_provisional_round_correction_recovers_from_sqlite_only(tmp_path) -> None:
    database_path = tmp_path / "edge-v12.db"
    first_flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
    )
    first = _edge("bearing_a", grade=0)
    second = _edge("bearing_b", grade=1)
    first_flow.apply_edge_result(
        first, _route(first, PacketRoute.EDGE), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=11,
    )
    first_flow.apply_edge_result(
        second, _route(second, PacketRoute.DEFER), expected_bearing_ids=("bearing_a", "bearing_b"), accepted_at_ns=12,
    )
    del first_flow
    recovered_flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
    )
    cloud = CloudBearingResult(
        result_id="cloud_dw_bearing_b_v1", review_id="review_01", device_id="machine_01",
        task_id="task_001", bearing_id="bearing_b", sender_id="sender_bearing_b", decision_round_id="round_01",
        diagnosis_window_id="dw_bearing_b", window_start_sequence=1, window_end_sequence=1,
        window_start_ns=0, window_end_ns=50_000_000, bearing_state="warning", confidence=.95,
        data_quality_score=.95, risk_level="medium", action_grade=2,
        recommended_action="scheduled_inspection", model_version="cloud_model_v1", created_at_ns=20,
    )

    _, correction = recovered_flow.apply_cloud_result(cloud, accepted_at_ns=21)

    assert correction is not None
    assert correction.status == "CORRECTED"
    assert correction.revision == 2
    assert correction.affects_realtime_action is False


def test_cloud_result_and_3500ms_round_timeout_have_one_database_winner(tmp_path) -> None:
    database_path = tmp_path / "edge-v12.db"
    initial_flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
    )
    edge = _edge("bearing_a", grade=0)
    initial_flow.apply_edge_result(
        edge,
        _route(edge, PacketRoute.CLOUD_NOW),
        expected_bearing_ids=("bearing_a",),
        accepted_at_ns=1,
    )
    cloud = CloudBearingResult(
        result_id="cloud_dw_bearing_a_v1", review_id="review_01", device_id="machine_01",
        task_id="task_001", bearing_id="bearing_a", sender_id="sender_bearing_a", decision_round_id="round_01",
        diagnosis_window_id="dw_bearing_a", window_start_sequence=1, window_end_sequence=1,
        window_start_ns=0, window_end_ns=50_000_000, bearing_state="warning", confidence=.95,
        data_quality_score=.95, risk_level="medium", action_grade=2,
        recommended_action="scheduled_inspection", model_version="cloud_model_v1", created_at_ns=3_000_000_000,
    )
    barrier = threading.Barrier(2)

    def accept_cloud():
        flow = V12DecisionFlow(
            BearingResultLifecycleManager(BearingResultRepository(database_path)),
            DeviceDecisionRoundRepository(database_path),
        )
        barrier.wait()
        return flow.apply_cloud_result(cloud, accepted_at_ns=3_000_000_000)

    def close_timeout():
        flow = V12DecisionFlow(
            BearingResultLifecycleManager(BearingResultRepository(database_path)),
            DeviceDecisionRoundRepository(database_path),
        )
        barrier.wait()
        return flow.finalize_timeouts(
            now_ns=3_500_000_001, round_timeout_ns=3_500_000_000
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cloud_future = executor.submit(accept_cloud)
        timeout_future = executor.submit(close_timeout)
        cloud_future.result()
        timeout_future.result()

    repository = DeviceDecisionRoundRepository(database_path)
    current = repository.get_current_result("machine_01", "task_001", "round_01")
    round_state = repository.get_round("machine_01", "task_001", "round_01")
    bearing = BearingResultRepository(database_path).get_current(
        "machine_01", "task_001", "round_01", "bearing_a"
    )
    assert current is not None and current.revision == 1
    assert round_state is not None and round_state["current_device_result_id"] == current.result_id
    if round_state["closure_reason"] == "ROUND_TIMEOUT":
        assert current.status == "INCOMPLETE"
        assert bearing is not None and bearing.lifecycle_state == "LATE_CLOUD_CORRECTED"
    else:
        assert round_state["closure_reason"] == "ALL_BEARINGS_FINAL"
        assert current.status == "FINAL"
        assert bearing is not None and bearing.lifecycle_state == "FINAL_CLOUD"


def test_cloud_result_at_persisted_deadline_seals_timeout_and_is_late(tmp_path) -> None:
    database_path = tmp_path / "edge-v12.db"
    published = []
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
        round_timeout_ns=100,
        on_device_result=published.append,
    )
    edge = _edge("bearing_a", grade=0)
    flow.apply_edge_result(
        edge,
        _route(edge, PacketRoute.CLOUD_NOW),
        expected_bearing_ids=("bearing_a",),
        accepted_at_ns=10,
    )
    cloud = CloudBearingResult(
        result_id="cloud_dw_bearing_a_v1", review_id="review_01",
        device_id="machine_01", task_id="task_001", bearing_id="bearing_a",
        sender_id="sender_bearing_a", decision_round_id="round_01",
        diagnosis_window_id="dw_bearing_a", window_start_sequence=1,
        window_end_sequence=1, window_start_ns=0, window_end_ns=50_000_000,
        bearing_state="warning", confidence=.95, data_quality_score=.95,
        risk_level="medium", action_grade=2,
        recommended_action="scheduled_inspection", model_version="cloud_model_v1",
        created_at_ns=110,
    )

    bearing, device = flow.apply_cloud_result(cloud, accepted_at_ns=110)

    assert bearing.lifecycle_state == "LATE_CLOUD_CORRECTED"
    assert device is not None and device.status == "INCOMPLETE"
    assert device.closure_reason == "ROUND_TIMEOUT"
    assert device.bearing_result_ids == ("bearing_round_01_bearing_a_r1",)
    assert published == [device]
    assert flow.device_rounds.get_current_result(
        "machine_01", "task_001", "round_01"
    ) == device


def test_same_task_persists_ten_independent_decision_rounds(tmp_path) -> None:
    database_path = tmp_path / "edge-v12.db"
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(BearingResultRepository(database_path)),
        DeviceDecisionRoundRepository(database_path),
    )
    result_ids: set[str] = set()
    for index in range(1, 11):
        round_id = f"round_{index:02d}"
        edge = replace(
            _edge("bearing_a", grade=index % 2),
            result_id=f"edge_{round_id}_bearing_a",
            decision_round_id=round_id,
            diagnosis_window_id=f"dw_{round_id}_bearing_a",
        )
        _, device = flow.apply_edge_result(
            edge,
            _route(edge, PacketRoute.EDGE),
            expected_bearing_ids=("bearing_a",),
            accepted_at_ns=index,
        )
        assert device is not None
        result_ids.add(device.result_id)

    assert len(result_ids) == 10
    for index in range(1, 11):
        round_id = f"round_{index:02d}"
        current = flow.device_rounds.get_current_result(
            "machine_01", "task_001", round_id
        )
        assert current is not None and current.revision == 1


def test_timeout_scan_closes_open_round_once_as_incomplete(tmp_path) -> None:
    bearing_repository = BearingResultRepository(tmp_path / "edge-v12.db")
    flow = V12DecisionFlow(
        BearingResultLifecycleManager(bearing_repository),
        DeviceDecisionRoundRepository(tmp_path / "edge-v12.db"),
        round_timeout_ns=100,
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


def test_runtime_config_allows_only_fixed_non_overlapping_diagnosis_windows() -> None:
    errors = EdgeRuntimeConfig(v12=V12RuntimeConfig(
        diagnosis_window_ms=100, diagnosis_step_ms=50, diagnosis_overlap_enabled=True,
    )).validate()

    assert "v12.diagnosis_step_ms must equal diagnosis_window_ms" in errors
    assert "v12.diagnosis_overlap_enabled must be false" in errors
    assert EdgeRuntimeConfig(v12=V12RuntimeConfig(
        diagnosis_window_ms=150, diagnosis_step_ms=150, diagnosis_overlap_enabled=False,
    )).validate() == []
