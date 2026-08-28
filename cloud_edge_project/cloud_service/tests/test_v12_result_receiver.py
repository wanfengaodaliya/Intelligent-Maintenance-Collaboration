from __future__ import annotations

from cloud_service.task_results import TaskResultService


def _bearing() -> dict:
    return {
        "result_id": "bearing_round_01_bearing_a_r1", "revision": 1, "replaces_result_id": None,
        "device_id": "machine_01", "task_id": "task_001", "bearing_id": "bearing_a",
        "sender_id": "sender_a", "decision_round_id": "round_01", "diagnosis_window_id": "dw_a",
        "lifecycle_state": "FINAL_EDGE", "bearing_state": "normal", "confidence": .9,
        "data_quality_score": 1.0, "risk_level": "low", "action_grade": 0,
        "recommended_action": "continue_operation", "decision_source": "FINAL_EDGE",
        "review_status": "NOT_REQUIRED", "degraded": False, "edge_result_id": "edge_dw_a_v1",
        "cloud_result_id": None, "model_version": "edge_model_v1", "created_at_ns": 10,
        "edge_accepted_at_ns": 11,
    }


def _device() -> dict:
    return {
        "result_id": "device_round_01_r1", "revision": 1, "replaces_result_id": None,
        "device_id": "machine_01", "task_id": "task_001", "decision_round_id": "round_01",
        "expected_bearing_ids": ["bearing_a"], "received_bearing_ids": ["bearing_a"],
        "missing_bearing_ids": [], "bearing_result_ids": ["bearing_round_01_bearing_a_r1"],
        "status": "FINAL", "closure_reason": "ALL_BEARINGS_FINAL", "final_state": "normal",
        "final_action_grade": 0, "final_action": "continue_operation", "confidence": .9,
        "data_quality_score": 1.0, "has_conflict": False, "conflict_reasons": [],
        "decision_source": "EDGE", "degraded": False, "affects_realtime_action": True,
        "arbitration_id": None, "closed_at_ns": 12, "created_at_ns": 12,
    }


def test_v12_result_receiver_is_idempotent_by_result_id_and_rejects_conflicts(tmp_path) -> None:
    service = TaskResultService(tmp_path / "cloud.db")
    bearing = _bearing()

    assert service.ingest_bearing_decision(bearing) == {"status": "accepted", "duplicate": False}
    assert service.ingest_bearing_decision(bearing) == {"status": "accepted", "duplicate": True}

    with_diagnosis = _bearing()
    with_diagnosis["result_id"] = "bearing_round_01_bearing_a_r2"
    with_diagnosis["diagnosis_label"] = "inner_ring_damage"
    with_diagnosis["class_probabilities"] = {
        "healthy": 0.05,
        "outer_ring_damage": 0.05,
        "inner_ring_damage": 0.9,
    }
    assert service.ingest_bearing_decision(with_diagnosis)["duplicate"] is False
    bearing["confidence"] = .8
    try:
        service.ingest_bearing_decision(bearing)
    except ValueError as error:
        assert str(error) == "RESULT_ID_CONFLICT"
    else:
        raise AssertionError("same result_id with another payload must conflict")


def test_v12_device_result_receiver_preserves_edge_revision_numbers(tmp_path) -> None:
    service = TaskResultService(tmp_path / "cloud.db")

    assert service.ingest_device_decision(_device()) == {"status": "accepted", "duplicate": False}
    stored = service.get_device_decision("device_round_01_r1")
    assert stored is not None
    assert stored["revision"] == 1
    assert stored["decision_round_id"] == "round_01"
