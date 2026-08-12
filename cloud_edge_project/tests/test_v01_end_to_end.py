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


def test_main_flow_uses_one_task_id_through_all_results():
    edge = infer_edge_v01(TASK)
    schedule = decide(
        {
            "task": TASK,
            "edge_result": edge,
            "network_state": {"latency_ms": 30, "bandwidth_mbps": 20, "packet_loss": 0.01, "cloud_available": True},
            "node_state": {"edge_cpu_usage": 0.55, "edge_memory_usage": 0.62, "cloud_queue_length": 3, "fog_available": True},
        }
    )
    cloud = infer_cloud_v01(
        {
            "task_id": TASK["task_id"],
            "scenario": TASK["scenario"],
            "task_type": TASK["task_type"],
            "source_node": TASK["source_node"],
            "data": TASK["data"],
            "edge_result": edge,
        }
    )

    assert schedule["route"] == "edge"
    assert {TASK["task_id"], edge["task_id"], schedule["task_id"], cloud["task_id"]} == {"task_0001"}
