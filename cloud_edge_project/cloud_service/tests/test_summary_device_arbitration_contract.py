from __future__ import annotations

import pytest
from fastapi.responses import JSONResponse

import cloud_service.app as cloud_api
from cloud_service.config import CloudSettings
from cloud_service.device_arbitration.summary_contract import (
    adapt_summary_arbitration_request,
    attach_summary_identity,
)
from core.arbitration_contracts import ArbitrationValidationError


ACTIONS = (
    "continue_operation",
    "enhanced_monitoring",
    "scheduled_inspection",
    "urgent_intervention",
    "shutdown",
)


def _bearing(bearing_id: str, edge_node_id: str, grade: int) -> dict:
    suffix = bearing_id[-2:]
    return {
        "result_id": f"edge_result_{suffix}",
        "device_id": "machine_01",
        "task_id": f"sd_{suffix}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{suffix}",
        "edge_node_id": edge_node_id,
        "window_start_sequence": 17,
        "window_end_sequence": 17,
        "bearing_state": "fault" if grade == 4 else "warning" if grade >= 2 else "normal",
        "confidence": 0.9,
        "data_quality_score": 0.8,
        "risk_level": "high" if grade >= 3 else "medium" if grade >= 1 else "low",
        "action_grade": grade,
        "recommended_action": ACTIONS[grade],
    }


def _request() -> dict:
    return {
        "conflict_id": "conflict_machine_01_17",
        "summary_result_id": "summary_machine_01_17",
        "device_id": "machine_01",
        "window_start_sequence": 17,
        "window_end_sequence": 17,
        "comparison": {
            "max_cross_edge_grade_gap": 3,
            "conflicting_pair_count": 2,
        },
        "bearing_results": [
            _bearing("bearing_01", "edge_01", 0),
            _bearing("bearing_02", "edge_02", 3),
            _bearing("bearing_03", "edge_01", 1),
        ],
    }


def _settings(database_path):
    return CloudSettings("moment_light_adapt", "", "", "", 1.0, database_path)


def test_summary_contract_recomputes_conflict_and_adapts_existing_algorithm() -> None:
    adapted = adapt_summary_arbitration_request(_request())

    assert adapted["conflict_id"] == "conflict_machine_01_17"
    assert adapted["subject_id"] == "machine_01"
    assert adapted["task_id"] == "summary_machine_01_17"
    assert adapted["scenario_payload"]["bearing_results"][0]["recommended_action"] == (
        "continue_operation"
    )
    assert adapted["summary_identity"]["comparison"] == {
        "max_cross_edge_grade_gap": 3,
        "conflicting_pair_count": 2,
    }
    attached = attach_summary_identity({"arbitration_id": "arb_01"}, adapted)
    assert attached["summary_result_id"] == "summary_machine_01_17"
    assert attached["window_start_sequence"] == 17


def test_summary_contract_rejects_a_false_reported_conflict() -> None:
    request = _request()
    request["comparison"]["conflicting_pair_count"] = 1

    with pytest.raises(ArbitrationValidationError, match="does not match"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_requires_three_bearings_from_two_edges() -> None:
    request = _request()
    request["bearing_results"][1]["edge_node_id"] = "edge_01"

    with pytest.raises(ArbitrationValidationError, match="at least two edge"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_requires_data_quality_score() -> None:
    request = _request()
    request["bearing_results"][0].pop("data_quality_score")

    with pytest.raises(ArbitrationValidationError, match="data_quality_score"):
        adapt_summary_arbitration_request(request)


def test_cloud_endpoint_persists_summary_identity_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))

    first = cloud_api.device_arbitration(_request())
    repeated = cloud_api.device_arbitration(_request())

    assert isinstance(first, dict)
    assert repeated == first
    assert first["conflict_id"] == "conflict_machine_01_17"
    assert first["summary_result_id"] == "summary_machine_01_17"
    assert first["bearing_result_ids"] == [
        "edge_result_01",
        "edge_result_02",
        "edge_result_03",
    ]


def test_cloud_endpoint_returns_409_for_same_conflict_id_with_new_payload(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))
    assert isinstance(cloud_api.device_arbitration(_request()), dict)
    changed = _request()
    changed["bearing_results"][0]["confidence"] = 0.7

    response = cloud_api.device_arbitration(changed)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
