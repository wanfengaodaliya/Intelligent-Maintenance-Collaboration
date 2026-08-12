import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_service.app import infer_cloud_v01
from common.schemas import ContractError, validate_task_log, validate_task_request
from edge_service.model import infer_edge_v01


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


def test_task_log_rejects_unsupported_route():
    payload = {**TASK_LOG, "route": "invalid_route"}

    with pytest.raises(ContractError):
        validate_task_log(payload)
