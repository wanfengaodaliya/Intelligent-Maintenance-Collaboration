"""Phase-one closed-loop integration (globel analysis): real infer -> persist -> analyze -> signal -> periodic save."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cloud_service.service as service_module
from cloud_service.config import load_cloud_settings
from cloud_service.global_analysis.periodic import run_all
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from cloud_service.moment_review_repository import MomentReviewRepository
from cloud_service.storage.database import initialize_database
from cloud_service.task_results import TaskResultService
from cloud_service.summary_windows import SummaryWindowRepository

MOMENT_ROUND_MARKER = "DEVEL_E2E"


class _FakeMomentRunner:
    loaded = True

    def predict(self, vibration, operating_context):
        return SimpleNamespace(
            label="outer_ring_damage", confidence=0.95, model_version="moment_e2e_v1"
        )


def _v12_request(window: dict) -> dict:
    return {
        "schema_version": "cloud-infer/2.0",
        "decision_round_id": service_module.build_decision_round_id(
            device_id=window["device_id"], task_id=window["task_id"],
            window_start_sequence=window["window_start_sequence"],
            window_end_sequence=window["window_end_sequence"],
        ),
        "diagnosis_window_id": service_module.build_diagnosis_window_id(
            device_id=window["device_id"], task_id=window["task_id"],
            bearing_id=window["bearing_id"], sender_id=window["sender_id"],
            window_start_sequence=window["window_start_sequence"],
            window_end_sequence=window["window_end_sequence"],
        ),
        "edge_perception_result": {
            "edge_inference": {"edge_result": "normal", "label": "normal", "confidence": 0.9},
            "features": {
                "operating_context": {
                    "shaft_speed_rpm": {"mean": 1200.0, "standard_deviation": 5.0, "minimum": 1100.0, "maximum": 1300.0},
                    "load_torque_nm": {"mean": 200.0, "standard_deviation": 4.0, "minimum": 180.0, "maximum": 220.0},
                    "bearing_radial_load_n": {"mean": 700.0, "standard_deviation": 5.0, "minimum": 650.0, "maximum": 750.0},
                    "bearing_module_temperature_c": 65.0,
                }
            },
        },
        "cloud_raw_window": window,
    }


def _window(*, device_id: str) -> dict:
    return {
        "device_id": device_id, "task_id": "task_001", "bearing_id": "bearing_02",
        "sender_id": "sender_02", "window_start_sequence": 1, "window_end_sequence": 1,
        "window_start_ns": 0, "window_end_ns": 50_000_000,
        "contributing_packet_ids": ["packet_001"], "sample_rate_hz": 64_000,
        "sample_count": 3_200,
        "data": {
            "vibration": {
                "sample_rate_hz": 64_000, "sample_count": 3_200,
                "values": [0.0] * 3_200,
            }
        },
    }


def _device_decision(*, device_id: str, decision_round_id: str) -> dict:
    return {
        "result_id": f"{MOMENT_ROUND_MARKER}_{device_id}_d1", "revision": 1,
        "replaces_result_id": None, "device_id": device_id, "task_id": "task_001",
        "decision_round_id": decision_round_id, "expected_bearing_ids": ["bearing_02"],
        "received_bearing_ids": ["bearing_02"], "missing_bearing_ids": [],
        "bearing_result_ids": [], "status": "FINAL", "closure_reason": "ROUND_TIMEOUT",
        "final_state": "fault", "final_action_grade": 2, "final_action": "scheduled_inspection",
        "confidence": .8, "data_quality_score": .9, "has_conflict": True,
        "conflict_reasons": ["EDGE_CLOUD_DIVERGENCE"], "decision_source": "EDGE",
        "degraded": False, "affects_realtime_action": True, "arbitration_id": None,
        "closed_at_ns": 30, "created_at_ns": 30,
    }


def _persist_moment_rows(database_path: Path, decision_round_id: str, count: int) -> None:
    repository = MomentReviewRepository(database_path)
    for index in range(count):
        repository.save({
            "review_id": f"moment_e2e_{index}", "result_id": f"cloud_e2e_{index}",
            "schema_version": "cloud-bearing-result/2.0", "device_id": "machine_01",
            "task_id": "task_001", "bearing_id": "bearing_02", "sender_id": "sender_02",
            "decision_round_id": decision_round_id, "diagnosis_window_id": f"dw_e2e_{index}",
            "window_start_sequence": 1, "window_end_sequence": 1,
            "window_start_ns": 0, "window_end_ns": 50_000_000,
            "bearing_state": "warning", "edge_label": "normal", "confidence": 0.95,
            "data_quality_score": 0.9, "risk_level": "medium", "action_grade": 2,
            "recommended_action": "scheduled_inspection", "model_version": "moment_e2e_v1",
            "created_at_ns": 10 + index,
        })


def test_phase_one_global_analysis_closes_the_loop(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "cloud.db"
    settings = replace(
        load_cloud_settings(),
        backend="moment_light_adapt",
        database_path=database_path,
    )
    initialize_database(database_path)
    monkeypatch.setattr(service_module, "get_moment_runner", lambda _settings: _FakeMomentRunner())

    # 1) 真实 moment 推理落库：edge_label 被补记到 cloud_moment_review_record。
    window = _window(device_id="machine_01")
    response = service_module.infer_cloud(_v12_request(window), settings)
    review = response["review_result"]
    assert review["edge_label"] == "normal"
    assert review["bearing_state"] == "warning"
    decision_round_id = review["decision_round_id"]

    # 2) 设备判决参考同一个 task/round（构造高冲突环境）。
    results = TaskResultService(database_path)
    assert results.ingest_device_decision(
        _device_decision(device_id="machine_01", decision_round_id=decision_round_id)
    )["duplicate"] is False
    SummaryWindowRepository(database_path).accept(
        {
            "summary_result_id": "summary_machine_01_1",
            "device_id": "machine_01",
            "window_start_sequence": 1,
            "window_end_sequence": 1,
            "result_status": "PENDING_ARBITRATION",
            "has_conflict": True,
            "excluded_from_formal_metrics": False,
            "max_cross_edge_grade_gap": 3,
            "conflicting_pair_count": 1,
            "closed_at_ns": 1,
        }
    )

    # 3) 凑足 packet 级样本量，让全局分析能输出 packet 模型更新的候选。
    _persist_moment_rows(database_path, decision_round_id, count=19)

    # 4) 周期 worker 对所有已知设备跑一轮全局分析并持久化。
    assert run_all(database_path, scenario_type="bearing") == ["machine_01"]

    saved = GlobalAnalysisResultRepository(database_path).get_latest("bearing", "machine_01")
    assert saved is not None

    packet = saved["packet_diagnosis_analysis"]
    assert packet["status"] == "succeeded"
    assert packet["reviewed_packet_count"] == 20
    assert packet["cloud_correction_count"] == 20
    assert packet["cloud_correction_rate"] == 1.0

    arbitration = saved["device_arbitration_analysis"]
    assert arbitration["conflict_rate"] == 1.0

    kinds = {
        (c["problem_layer"], c["problem_type"]): c["suggested_action"]
        for c in saved["problem_candidates"]
    }
    # packet 级修正 → 模型更新候选
    assert ("packet_diagnosis", "risk_underestimation") in kinds
    assert kinds[("packet_diagnosis", "risk_underestimation")] == "model_update"
    # 仲裁高冲突 → 模型更新信号候选
    assert ("device_arbitration", "high_conflict_rate") in kinds
    assert kinds[("device_arbitration", "high_conflict_rate")] == "arbitration_rule_review"
    assert ("device_arbitration", "high_conflict_rate_model") in kinds
    assert kinds[("device_arbitration", "high_conflict_rate_model")] == "model_update"
