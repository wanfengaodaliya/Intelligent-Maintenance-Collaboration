import pytest

from cloud_service.edge_status_registry import EdgeStatusRegistry, EdgeStatusValidationError


def _report(reported_at_ns: int = 1) -> dict:
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


def test_cloud_stores_and_returns_latest_edge_status():
    registry = EdgeStatusRegistry()

    accepted = registry.update(_report())

    assert accepted == {
        "edge_node_id": "edge_01",
        "accepted": True,
        "reported_at_ns": 1,
    }
    assert registry.get("edge_01") == _report()


def test_cloud_rejects_stale_edge_status_without_overwriting_latest():
    registry = EdgeStatusRegistry()
    registry.update(_report(2))

    stale = registry.update(_report(1))

    assert stale["accepted"] is False
    assert stale["reason_code"] == "STALE_STATUS_REPORT"
    assert registry.get("edge_01")["reported_at_ns"] == 2


def test_cloud_rejects_invalid_edge_status():
    registry = EdgeStatusRegistry()
    payload = _report()
    del payload["resources"]["queue_length"]

    with pytest.raises(EdgeStatusValidationError, match="queue_length"):
        registry.update(payload)
