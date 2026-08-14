from __future__ import annotations

import pytest

import cloud_service.app as cloud_api
from cloud_service.config import CloudSettings
from cloud_service.device_arbitration.v12_contract import (
    adapt_v12_device_arbitration_request,
    attach_v12_identity,
)
from core.arbitration_contracts import ArbitrationValidationError


def _request() -> dict:
    return {
        "conflict_id": "conflict_device_01",
        "device_id": "machine_01",
        "task_id": "task_001",
        "decision_round_id": "round_machine_01_task_001_0001",
        "device_result_revision": 2,
        "bearing_result_ids": ["bearing_a_r2", "bearing_b_r2"],
        "bearing_results": [
            {
                "bearing_id": "bearing_a",
                "bearing_result_id": "bearing_a_r2",
                "confidence": 0.7,
                "risk_level": "MEDIUM",
                "action_level": 1,
            },
            {
                "bearing_id": "bearing_b",
                "bearing_result_id": "bearing_b_r2",
                "confidence": 0.6,
                "risk_level": "HIGH",
                "action_level": 3,
            },
        ],
        "comparison": {"conflict": True},
        "local_arbitration_supported": True,
    }


def test_v12_device_arbitration_adapts_and_returns_identity() -> None:
    adapted = adapt_v12_device_arbitration_request(_request())

    assert adapted["conflict_id"] == "conflict_device_01"
    assert adapted["subject_id"] == "machine_01"
    assert adapted["scenario_payload"]["bearing_results"][0]["recommended_action"] == (
        "enhanced_monitoring"
    )
    assert attach_v12_identity({"arbitration_id": "arb_01"}, adapted) == {
        "arbitration_id": "arb_01",
        "device_id": "machine_01",
        "task_id": "task_001",
        "decision_round_id": "round_machine_01_task_001_0001",
        "device_result_revision": 2,
        "bearing_result_ids": ["bearing_a_r2", "bearing_b_r2"],
    }


def test_v12_device_arbitration_rejects_mismatched_result_identity() -> None:
    request = _request()
    request["bearing_result_ids"] = ["bearing_b_r2", "bearing_a_r2"]

    with pytest.raises(ArbitrationValidationError, match="bearing_result_ids"):
        adapt_v12_device_arbitration_request(request)


def test_cloud_endpoint_accepts_scheduler_v12_arbitration_payload(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        cloud_api,
        "load_cloud_settings",
        lambda: CloudSettings("mock", "", "", "", 1.0, tmp_path / "cloud.db"),
    )

    result = cloud_api.device_arbitration(_request())

    assert isinstance(result, dict)
    assert result["conflict_id"] == "conflict_device_01"
    assert result["decision_round_id"] == "round_machine_01_task_001_0001"
    assert result["device_result_revision"] == 2
    assert result["bearing_result_ids"] == ["bearing_a_r2", "bearing_b_r2"]
