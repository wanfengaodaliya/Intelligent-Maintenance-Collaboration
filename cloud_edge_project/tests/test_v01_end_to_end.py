import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_service.app import infer_cloud_v01
from edge_service.model import infer_edge_v01
from scheduler.api import decide


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


def run_main_flow(task):
    edge = infer_edge_v01(task)
    schedule = decide(
        {
            "task": task,
            "edge_result": edge,
            "network_state": {"latency_ms": 30, "bandwidth_mbps": 20, "packet_loss": 0.01, "cloud_available": True},
            "node_state": {"edge_cpu_usage": 0.55, "edge_memory_usage": 0.62, "cloud_queue_length": 3, "fog_available": True},
        }
    )
    cloud = None
    if schedule["route"] == "cloud":
        cloud = infer_cloud_v01(
            {
                "task_id": task["task_id"],
                "scenario": task["scenario"],
                "task_type": task["task_type"],
                "source_node": task["source_node"],
                "data": task["data"],
                "edge_result": edge,
            }
        )
    return edge, schedule, cloud


def test_cloud_route_uses_one_task_id_through_cloud_result():
    low_confidence_task = {
        **TASK,
        "data": {**TASK["data"], "current": 1.0, "load": 0.4},
    }

    edge, schedule, cloud = run_main_flow(low_confidence_task)

    assert schedule["route"] == "cloud"
    assert cloud is not None
    assert {low_confidence_task["task_id"], edge["task_id"], schedule["task_id"], cloud["task_id"]} == {"task_0001"}


def test_edge_route_does_not_produce_a_cloud_result():
    edge, schedule, cloud = run_main_flow(TASK)

    assert schedule["route"] == "edge"
    assert cloud is None
    assert {TASK["task_id"], edge["task_id"], schedule["task_id"]} == {"task_0001"}
