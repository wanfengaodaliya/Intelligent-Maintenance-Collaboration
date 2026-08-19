import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import logger
from common.logger import append_task_trace, compute_metrics, read_task_traces
from log_service import app as log_service_app


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
    "final_label": "fault",
    "final_confidence": 0.93,
    "success": True,
    "has_conflict": False,
    "conflict_resolved": None,
    "timestamp": "2026-06-20 10:00:05",
}

LEGACY_TRACE = {
    "packet_id": "packet_0001",
    "device_id": "machine_01",
    "sensor_id": "sensor_01",
    "sequence_number": 1,
    "data_type": "bearing_timeseries",
    "route": "edge",
    "edge_label": "normal",
    "edge_confidence": 0.81,
    "cloud_confidence": None,
    "final_label": "normal",
    "final_confidence": 0.81,
    "risk_level": "low",
    "edge_latency_ms": 80,
    "network_latency_ms": None,
    "cloud_latency_ms": None,
    "total_latency_ms": 80,
    "success": True,
    "log_timestamp": "2026-06-20T10:00:06",
}


@pytest.fixture(autouse=True)
def isolate_log_root(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "PROJECT_ROOT", tmp_path)


def config_for():
    return {"log": {"path": "logs/task_trace.jsonl"}}


def log_with_latency(latency):
    return {**TASK_LOG, "total_latency_ms": latency}


def test_v01_log_is_saved_and_included_in_metrics():
    config = config_for()

    saved = append_task_trace(TASK_LOG, config)
    metrics = compute_metrics(read_task_traces(config))

    assert saved == {"task_id": "task_0001", "saved": True, "log_path": "logs/task_trace.jsonl"}
    assert metrics["avg_latency_ms"] == 154.0
    assert metrics["cloud_call_ratio"] == 1.0
    assert metrics["conflict_rate"] == 0.0


def test_metrics_use_nearest_rank_p95():
    assert compute_metrics([log_with_latency(10), log_with_latency(30)])["p95_latency_ms"] == 30.0


def test_metrics_keep_v01_and_legacy_metric_keys_for_an_empty_collection():
    metrics = compute_metrics([])

    assert {
        "avg_latency_ms",
        "p95_latency_ms",
        "cloud_call_ratio",
        "edge_only_ratio",
        "weak_network_availability",
        "conflict_rate",
        "conflict_resolve_rate",
        "success_rate",
        "total_packets",
        "avg_total_latency_ms",
        "fallback_edge_ratio",
        "abnormal_ratio",
    }.issubset(metrics)
    assert all(value == 0.0 or value == 0 for value in metrics.values())


def test_metrics_include_v01_and_legacy_records_together():
    metrics = compute_metrics([TASK_LOG, LEGACY_TRACE])

    assert metrics["total_packets"] == 2
    assert metrics["avg_latency_ms"] == 117.0
    assert metrics["cloud_call_ratio"] == 0.5
    assert metrics["edge_only_ratio"] == 0.5
    assert metrics["success_rate"] == 1.0


def test_edge_cloud_counts_as_a_cloud_call_but_not_edge_only_processing():
    metrics = compute_metrics([
        {**TASK_LOG, "task_id": "task_edge_cloud", "route": "edge_cloud"},
        {**TASK_LOG, "task_id": "task_edge", "route": "edge"},
    ])

    assert metrics["cloud_call_ratio"] == 0.5
    assert metrics["edge_only_ratio"] == 0.5


def test_conflict_rates_use_all_tasks_and_only_conflicting_tasks_as_denominators():
    traces = [
        {**TASK_LOG, "task_id": "task_1", "has_conflict": True, "conflict_resolved": True},
        {**TASK_LOG, "task_id": "task_2", "has_conflict": True, "conflict_resolved": False},
        {**TASK_LOG, "task_id": "task_3", "has_conflict": False, "conflict_resolved": None},
    ]

    metrics = compute_metrics(traces)

    assert metrics["conflict_rate"] == 0.6667
    assert metrics["conflict_resolve_rate"] == 0.5


def test_weak_network_availability_uses_only_fallback_edge_tasks_as_denominator():
    traces = [
        {**TASK_LOG, "task_id": "task_1", "route": "fallback_edge", "success": True},
        {**TASK_LOG, "task_id": "task_2", "route": "fallback_edge", "success": False},
        {**TASK_LOG, "task_id": "task_3", "route": "cloud", "success": True},
    ]

    assert compute_metrics(traces)["weak_network_availability"] == 0.5
    assert compute_metrics([{**TASK_LOG, "route": "cloud"}])["weak_network_availability"] == 0.0


def test_dashboard_tasks_projects_v01_final_results_and_legacy_records(monkeypatch):
    config = config_for()
    append_task_trace(TASK_LOG, config)
    append_task_trace(LEGACY_TRACE, config)
    monkeypatch.setattr(log_service_app, "config", config)

    response = TestClient(log_service_app.app).get("/dashboard/tasks")

    assert response.status_code == 200
    v01_row, legacy_row = response.json()["tasks"]
    assert v01_row == {
        "task_id": "task_0001",
        "scenario": "industrial",
        "source_node": "edge_1",
        "route": "cloud",
        "label": "fault",
        "confidence": 0.93,
        "total_latency_ms": 154,
        "success": True,
        "timestamp": "2026-06-20 10:00:05",
    }
    assert legacy_row == {
        "packet_id": "packet_0001",
        "device_id": "machine_01",
        "route": "edge",
        "final_label": "normal",
        "final_confidence": 0.81,
        "risk_level": "low",
        "total_latency_ms": 80,
        "success": True,
        "log_timestamp": "2026-06-20T10:00:06",
    }
