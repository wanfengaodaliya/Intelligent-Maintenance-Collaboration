from __future__ import annotations

from pathlib import Path

from cloud_service.global_analysis.periodic import list_subject_ids, run_all
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from cloud_service.task_results import TaskResultService


def _device(*, device_id: str, result_id: str, revision: int) -> dict:
    return {
        "result_id": result_id, "revision": revision, "replaces_result_id": None,
        "device_id": device_id, "task_id": "task_001", "decision_round_id": "round_001",
        "expected_bearing_ids": ["bearing_a"], "received_bearing_ids": ["bearing_a"],
        "missing_bearing_ids": [], "bearing_result_ids": [], "status": "FINAL",
        "closure_reason": "ROUND_TIMEOUT", "final_state": "fault",
        "final_action_grade": 3, "final_action": "urgent_intervention", "confidence": .8,
        "data_quality_score": .9, "has_conflict": False, "conflict_reasons": [],
        "decision_source": "EDGE", "degraded": True, "affects_realtime_action": True,
        "arbitration_id": None, "closed_at_ns": 30, "created_at_ns": 30,
    }


def test_list_subject_ids_returns_distinct_devices(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    results = TaskResultService(database_path)
    assert results.ingest_device_decision(_device(device_id="machine_01", result_id="d1", revision=1))["duplicate"] is False
    assert results.ingest_device_decision(_device(device_id="machine_02", result_id="d2", revision=1))["duplicate"] is False
    assert results.ingest_device_decision(_device(device_id="machine_01", result_id="d3", revision=2))["duplicate"] is False

    assert list_subject_ids(database_path) == ["machine_01", "machine_02"]


def test_list_subject_ids_empty_when_no_decisions(tmp_path: Path) -> None:
    assert list_subject_ids(tmp_path / "empty.db") == []


def test_run_all_analyses_every_subject_and_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    results = TaskResultService(database_path)
    assert results.ingest_device_decision(_device(device_id="machine_01", result_id="d1", revision=1))["duplicate"] is False
    assert results.ingest_device_decision(_device(device_id="machine_02", result_id="d2", revision=1))["duplicate"] is False

    succeeded = run_all(database_path, scenario_type="bearing")

    assert sorted(succeeded) == ["machine_01", "machine_02"]
    repository = GlobalAnalysisResultRepository(database_path)
    for subject in succeeded:
        assert repository.get_latest("bearing", subject) is not None