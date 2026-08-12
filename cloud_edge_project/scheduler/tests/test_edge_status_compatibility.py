from scheduler.node_registry import EdgeNodeConfig, NodeRegistry


def _report(reported_at_ns: int) -> dict:
    return {
        "edge_node_id": "edge_01",
        "reported_at_ns": reported_at_ns,
        "resources": {
            "logical_cpu_count": 8,
            "cpu_utilization_percent": 20.0,
            "memory_available_mb": 4096.0,
            "gpu_available": False,
            "npu_available": False,
            "queue_length": 0,
        },
        "models": [
            {"model_version": "bearing_packet_model_v1", "load_status": "LOADED"}
        ],
        "last_task_activity_ns": 0,
    }


def test_scheduler_accepts_reporter_payload_without_network_status():
    registry = NodeRegistry(
        {
            "edge_01": EdgeNodeConfig(
                "edge_01",
                "http://127.0.0.1:8001",
                "edge/edge_01/input",
            )
        }
    )

    first = registry.update_status(
        _report(1),
        received_at_ns=1,
        received_monotonic_ns=1,
    )
    second = registry.update_status(
        _report(2),
        received_at_ns=2,
        received_monotonic_ns=2,
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["status"] == "ONLINE"
