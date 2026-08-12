import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_service import app as cloud_service_app
from cloud_service.app import infer_cloud_v01
from common.schemas import ContractError, validate_task_log, validate_task_request
from edge_service import app as edge_service_app
from edge_service.model import EDGE_NODE_ID, infer_edge_v01


TASK = {
    "task_id": "task_0001",
    "scenario": "industrial",
    "source_node": "edge_1",
    "task_type": "fault_detection",
    "timestamp": "2026-06-20 10:00:00",
    "deadline_ms": 200,
    "priority": 0.8,
    "data_size_kb": 128,
    "data": {
        "device_id": "machine_01",
        "temperature": 78.5,
        "vibration": 0.63,
        "current": 13.2,
        "load": 0.76,
    },
}

CLOUD_REQUEST = {
    "task_id": "task_0001",
    "scenario": "industrial",
    "task_type": "fault_detection",
    "source_node": "edge_1",
    "data": TASK["data"],
    "edge_result": {
        "label": "abnormal",
        "confidence": 0.72,
        "risk_level": "medium",
    },
}

TASK_LOG = {
    "task_id": "task_0001",
    "scenario": "industrial",
    "source_node": "edge_1",
    "route": "cloud",
    "edge_latency_ms": 38,
    "network_latency_ms": 30,
    "cloud_latency_ms": 86,
    "total_latency_ms": 154,
    "edge_confidence": 0.72,
    "cloud_confidence": 0.93,
    "success": True,
    "has_conflict": False,
    "conflict_resolved": None,
    "timestamp": "2026-06-20 10:00:05",
}

LEGACY_EDGE_PACKET = {
    "packet_id": "packet_0001",
    "task_id": "legacy_task_0001",
    "device_id": "machine_01",
    "sensor_id": "sensor_01",
    "sequence_number": 1,
    "start_timestamp_ns": 1_000_000,
    "end_timestamp_ns": 1_050_000,
    "duration_ms": 50,
    "data": {
        "data_type": "bearing_timeseries",
        "vibration_sample_rate_hz": 16000,
        "vibration_sample_count": 800,
        "vibration": [0.01] * 800,
        "current": 0.8,
        "temperature": 30.0,
        "speed": 1200.0,
        "load": 0.4,
    },
}


def test_edge_v01_returns_documented_contract():
    result = infer_edge_v01(TASK)

    assert result["task_id"] == "task_0001"
    assert set(result) == {
        "task_id",
        "node_id",
        "model_name",
        "label",
        "confidence",
        "risk_level",
        "edge_latency_ms",
        "need_cloud",
    }
    assert (result["label"], result["confidence"], result["risk_level"], result["need_cloud"]) == (
        "abnormal",
        0.92,
        "high",
        False,
    )


def test_edge_v01_reports_actual_edge_service_node():
    result = infer_edge_v01(TASK)

    assert result["node_id"] == EDGE_NODE_ID


def test_cloud_v01_returns_documented_contract():
    result = infer_cloud_v01(CLOUD_REQUEST)

    assert result["task_id"] == "task_0001"
    assert set(result) == {
        "task_id",
        "node_id",
        "model_name",
        "label",
        "confidence",
        "risk_level",
        "cloud_latency_ms",
        "decision",
    }
    assert set(result["decision"]) == {"action", "description"}


def test_task_request_rejects_out_of_range_priority():
    payload = {**TASK, "priority": 1.1}

    with pytest.raises(ContractError):
        validate_task_request(payload)


def test_task_request_rejects_missing_industrial_sensor_value():
    data = {key: value for key, value in TASK["data"].items() if key != "temperature"}

    with pytest.raises(ContractError):
        infer_edge_v01({**TASK, "data": data})


def test_task_log_rejects_unsupported_route():
    payload = {**TASK_LOG, "route": "invalid_route"}

    with pytest.raises(ContractError):
        validate_task_log(payload)


def test_edge_http_uses_v01_contract_for_complete_task_request():
    response = TestClient(edge_service_app.app).post("/edge/infer", json=TASK)

    assert response.status_code == 200
    assert response.json()["task_id"] == "task_0001"


def test_edge_http_keeps_legacy_packet_with_task_id_on_legacy_path():
    response = TestClient(edge_service_app.app).post("/edge/infer", json=LEGACY_EDGE_PACKET)

    assert response.status_code == 200
    assert response.json()["packet_id"] == "packet_0001"


def test_edge_http_returns_400_when_v01_sensor_value_is_missing():
    data = {key: value for key, value in TASK["data"].items() if key != "temperature"}

    response = TestClient(edge_service_app.app).post("/edge/infer", json={**TASK, "data": data})

    assert response.status_code == 400
    assert response.json()["error_code"] == "MISSING_FIELD"


def test_cloud_http_keeps_legacy_payload_with_task_id_on_legacy_path(monkeypatch):
    class LegacyHandler:
        def infer(self, payload):
            return {"legacy_task_id": payload["task_id"]}

    monkeypatch.setattr(
        cloud_service_app,
        "get_scenario_handler",
        lambda *_args, **_kwargs: LegacyHandler(),
    )

    response = TestClient(cloud_service_app.app).post(
        "/cloud/infer",
        json={
            "task_id": "legacy_task_0001",
            "scenario_type": "bearing",
            "cloud_raw_packet": {},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"legacy_task_id": "legacy_task_0001"}
