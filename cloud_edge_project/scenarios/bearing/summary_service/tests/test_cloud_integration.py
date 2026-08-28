from __future__ import annotations

from cloud_service.device_arbitration.service import DeviceArbitrationService
from cloud_service.device_arbitration.summary_contract import (
    adapt_summary_arbitration_request,
    attach_summary_identity,
)
from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import GlobalAnalysisConfig
from cloud_service.global_analysis.v12_data_source import V12GlobalAnalysisDataSource
from cloud_service.summary_windows import SummaryWindowRepository
from scenarios.bearing.cloud.device_arbitration.adapter import BearingDeviceArbitrationAdapter
from summary_service.outbox import ArbitrationOutbox
from summary_service.repository import SummaryRepository
from summary_service.service import SummaryService


ACTIONS = (
    "continue_operation",
    "enhanced_monitoring",
    "scheduled_inspection",
    "urgent_intervention",
    "shutdown",
)

LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low"),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low"),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high"),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high"),
}

LEGACY_GRADE = {0: 0, 1: 1, 2: 2, 3: 4}


def bearing(bearing_id: str, edge_node_id: str, action_level: int) -> dict:
    suffix = bearing_id[-2:]
    probabilities, risk_level = LEVEL_PROBS[action_level]
    grade = LEGACY_GRADE[action_level]
    return {
        "result_id": f"result_{suffix}",
        "device_id": "machine_01",
        "task_id": f"sd_{suffix}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{suffix}",
        "edge_node_id": edge_node_id,
        "run_id": "run_01",
        "decision_round_id": f"round_{suffix}",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "bearing_state": "fault" if action_level == 3 else "normal",
        "risk_level": risk_level,
        "action_grade": grade,
        "recommended_action": ACTIONS[grade],
        "confidence": 0.9,
        "data_quality_score": 1.0,
        "model_version": "model-test",
        "created_at_ns": 100,
        "class_probabilities": probabilities,
    }


def test_summary_to_cloud_arbitration_and_global_analysis_loop(tmp_path):
    summary_repository = SummaryRepository(tmp_path / "summary.db")
    summary_service = SummaryService(summary_repository, now_ns=lambda: 1_000)
    for payload in (
        bearing("bearing_01", "edge_01", 0),
        bearing("bearing_02", "edge_02", 3),
    ):
        summary_service.ingest(payload)

    cloud_database = tmp_path / "cloud.db"
    cloud_windows = SummaryWindowRepository(cloud_database)
    cloud_arbitration = DeviceArbitrationService(
        cloud_database, BearingDeviceArbitrationAdapter()
    )

    sync = ArbitrationOutbox(
        summary_repository,
        cloud_windows.accept,
        now_ns=lambda: 2_000,
        table_name="summary_window_sync_outbox",
    )

    def arbitrate(payload):
        adapted = adapt_summary_arbitration_request(payload)
        return attach_summary_identity(cloud_arbitration.arbitrate(adapted), adapted)

    arbitration = ArbitrationOutbox(
        summary_repository, arbitrate, now_ns=lambda: 2_000
    )

    assert sync.run_due() == 1
    assert arbitration.run_due() == 1
    assert arbitration.run_due() == 0

    data = V12GlobalAnalysisDataSource(cloud_database).load("machine_01", 20)
    analysis = analyze_device_arbitration(
        data["summary_windows"], data["arbitrations"], GlobalAnalysisConfig()
    )
    assert analysis["complete_window_count"] == 1
    assert analysis["conflict_rate"] == 1.0
    assert analysis["arbitration_count"] == 1
    assert analysis["arbitration_success_rate"] == 1.0
    assert analysis["arbitration_upload_success_rate"] == 1.0
    assert summary_repository.metrics()["arbitration_upload_success_rate"] == 1.0
