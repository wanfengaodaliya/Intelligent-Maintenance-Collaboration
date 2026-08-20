import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# AUD-12: these end-to-end tests import the Edge application, which loads the
# H5 diagnostic model (torch). Run them in the Conda "moment" environment;
# torch-less .venv runs skip them explicitly.
pytest.importorskip(
    "torch",
    reason="V0.1 e2e tests import the torch-based Edge app (Conda moment env)",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_service import app as cloud_app
from common import logger
from edge_service import app as edge_app
from log_service import app as log_app
from scheduler import api as scheduler_api
from scheduler.rule_scheduler import decide_schedule


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
        "current": 1.0,
        "load": 0.4,
    },
}


def test_real_http_chain_persists_final_result_and_dashboard_metrics(monkeypatch, tmp_path):
    log_config = {"log": {"path": "logs/v01-e2e.jsonl"}}
    monkeypatch.setattr(logger, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(log_app, "config", log_config)

    edge = TestClient(edge_app.app).post("/edge/infer", json=TASK).json()
    schedule = TestClient(scheduler_api.app).post(
        "/scheduler/decide",
        json={
            "task": {
                key: TASK[key]
                for key in (
                    "task_id",
                    "scenario",
                    "task_type",
                    "deadline_ms",
                    "priority",
                    "data_size_kb",
                )
            },
            "edge_result": edge,
            "network_state": {
                "latency_ms": 30,
                "bandwidth_mbps": 20,
                "packet_loss": 0.01,
                "cloud_available": True,
            },
            "node_state": {
                "edge_cpu_usage": 0.55,
                "edge_memory_usage": 0.62,
                "cloud_queue_length": 3,
                "fog_available": True,
            },
        },
    ).json()
    assert schedule["route"] == "cloud"

    cloud = TestClient(cloud_app.app).post(
        "/cloud/infer",
        json={
            "task_id": TASK["task_id"],
            "scenario": TASK["scenario"],
            "task_type": TASK["task_type"],
            "source_node": TASK["source_node"],
            "data": TASK["data"],
            "edge_result": edge,
        },
    ).json()
    trace = {
        "task_id": TASK["task_id"],
        "scenario": TASK["scenario"],
        "source_node": TASK["source_node"],
        "route": schedule["route"],
        "edge_latency_ms": edge["edge_latency_ms"],
        "network_latency_ms": 30,
        "cloud_latency_ms": cloud["cloud_latency_ms"],
        "total_latency_ms": edge["edge_latency_ms"] + 30 + cloud["cloud_latency_ms"],
        "edge_confidence": edge["confidence"],
        "cloud_confidence": cloud["confidence"],
        "final_label": cloud["label"],
        "final_confidence": cloud["confidence"],
        "success": True,
        "has_conflict": False,
        "conflict_resolved": None,
        "timestamp": "2026-06-20 10:00:05",
    }
    log_client = TestClient(log_app.app)
    saved = log_client.post("/logs/task_trace", json=trace)
    metrics = log_client.get("/dashboard/metrics").json()
    row = log_client.get("/dashboard/tasks").json()["tasks"][0]

    assert saved.status_code == 200
    assert {
        edge["task_id"],
        schedule["task_id"],
        cloud["task_id"],
        saved.json()["task_id"],
        row["task_id"],
    } == {TASK["task_id"]}
    assert (row["label"], row["confidence"]) == (cloud["label"], cloud["confidence"])
    assert metrics["total_packets"] == 1
    assert metrics["cloud_call_ratio"] == 1.0
    assert metrics["success_rate"] == 1.0


def test_legacy_direct_scheduler_keeps_high_packet_loss_fallback_and_internal_fields():
    result = decide_schedule(
        {
            "task": {
                "task_id": "legacy_task_0001",
                "source_node": "edge_1",
                "deadline_ms": 200,
                "priority": 0.8,
                "data_size_kb": 128,
            },
            "edge_result": {
                "confidence": 0.72,
                "need_cloud": True,
                "edge_latency_ms": 38,
            },
            "network_state": {
                "latency_ms": 30,
                "bandwidth_mbps": 20,
                "packet_loss": 0.5,
                "cloud_available": True,
            },
            "node_state": {
                "edge_cpu_usage": 0.55,
                "edge_memory_usage": 0.62,
                "cloud_queue_length": 3,
            },
        }
    )

    assert result["route"] == "fallback_edge"
    assert result["scheduler"] == "PER-DDPG-rule-minimal"
    assert set(result["policy_score"]) == {"edge", "cloud"}
