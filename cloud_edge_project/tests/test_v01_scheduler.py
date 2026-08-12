import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scheduler import api as scheduler_api


def schedule_request(cloud_available: bool, confidence: float) -> dict:
    return {
        "task": {
            "task_id": "task_0001",
            "scenario": "industrial",
            "task_type": "fault_detection",
            "deadline_ms": 200,
            "priority": 0.8,
            "data_size_kb": 128,
        },
        "edge_result": {
            "label": "abnormal",
            "confidence": confidence,
            "edge_latency_ms": 38,
            "need_cloud": False,
        },
        "network_state": {
            "latency_ms": 30,
            "bandwidth_mbps": 20,
            "packet_loss": 0.01,
            "cloud_available": cloud_available,
        },
        "node_state": {
            "edge_cpu_usage": 0.55,
            "edge_memory_usage": 0.62,
            "cloud_queue_length": 3,
            "fog_available": True,
        },
    }


@pytest.mark.parametrize(("cloud_available", "confidence", "route"), [
    (False, 0.5, "fallback_edge"),
    (True, 0.79, "cloud"),
    (True, 0.80, "edge"),
])
def test_documented_scheduler_rules(cloud_available, confidence, route):
    request = schedule_request(cloud_available, confidence)

    decision = scheduler_api.decide(request)

    assert decision["route"] == route
    assert set(decision) == {
        "task_id",
        "route",
        "target_node",
        "reason",
        "estimated_total_latency_ms",
        "upload_required",
    }


def test_sender_assignment_requests_keep_assignment_response(monkeypatch):
    sender_request = {
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_size_bytes": 100000,
        "expected_packet_count": 80,
        "expected_duration_ms": 4000,
        "created_timestamp_ns": 1781920800000000000,
    }
    expected = {
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "target_topic": "edge/edge_01/input",
    }

    class ExistingAssignmentScheduler:
        def decide(self, request):
            return SimpleNamespace(to_dict=lambda: expected)

    monkeypatch.setattr(scheduler_api, "scheduler", ExistingAssignmentScheduler())

    assert scheduler_api.decide(sender_request) == expected
