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


def test_cloud_preserves_optional_network_status() -> None:
    registry = EdgeStatusRegistry()
    report = _report()
    report["network_to_scheduler"] = {
        "measured_at_ns": 123,
        "available_uplink_mbps_estimate": 25.0,
        "rtt_ms_avg": 12.0,
        "rtt_ms_p95": 16.0,
        "loss_rate": 0.005,
    }

    registry.update(report)

    assert registry.get("edge_01") == report


def test_cloud_preserves_memory_measurement_status() -> None:
    """EDGE-3: Cloud registry 存储 memory_measurement_status。"""
    registry = EdgeStatusRegistry()
    report = _report()
    report["resources"]["memory_measurement_status"] = "DEGRADED"

    registry.update(report)

    stored = registry.get("edge_01")["resources"]
    assert stored["memory_measurement_status"] == "DEGRADED"


def test_cloud_missing_memory_measurement_status_stays_compatible() -> None:
    """EDGE-3: 旧报告缺失 memory 字段，不写入，保持旧投影。"""
    registry = EdgeStatusRegistry()

    registry.update(_report())

    resources = registry.get("edge_01")["resources"]
    assert "memory_measurement_status" not in resources


def test_cloud_rejects_unknown_memory_measurement_status() -> None:
    """EDGE-3: 非法 memory 状态与 cpu/queue 同等拒绝。"""
    registry = EdgeStatusRegistry()
    report = _report()
    report["resources"]["memory_measurement_status"] = "SUSPICIOUS"

    with pytest.raises(EdgeStatusValidationError, match="memory_measurement_status"):
        registry.update(report)


def test_cloud_preserves_network_measurement_status_fields() -> None:
    registry = EdgeStatusRegistry()
    report = _report()
    report["network_to_scheduler"] = {
        "measured_at_ns": 123,
        "available_uplink_mbps_estimate": 25.0,
        "rtt_ms_avg": 12.0,
        "rtt_ms_p95": 16.0,
        "loss_rate": 0.005,
        "measurement_status": "STALE",
        "rtt_p95_is_estimate": True,
        "last_successful_measurement_ns": 100,
    }

    registry.update(report)

    stored = registry.get("edge_01")["network_to_scheduler"]
    assert stored["measurement_status"] == "STALE"
    assert stored["rtt_p95_is_estimate"] is True
    assert stored["last_successful_measurement_ns"] == 100


def test_cloud_rejects_unknown_network_measurement_status() -> None:
    registry = EdgeStatusRegistry()
    report = _report()
    report["network_to_scheduler"] = {
        "measured_at_ns": 123,
        "available_uplink_mbps_estimate": 25.0,
        "rtt_ms_avg": 12.0,
        "rtt_ms_p95": 16.0,
        "loss_rate": 0.005,
        "measurement_status": "GREAT",
    }

    with pytest.raises(EdgeStatusValidationError, match="measurement_status"):
        registry.update(report)


def test_cloud_preserves_resource_measurement_status_fields() -> None:
    """AUD-03/04/11: cpu/accelerator/queue 的采集状态必须随报告一起保存。"""
    registry = EdgeStatusRegistry()
    report = _report()
    report["resources"].update(
        {
            "cpu_measurement_status": "DEGRADED",
            "accelerator_measurement_status": "STALE",
            "queue_measurement_status": "FAILED",
            "queue_breakdown": {
                "ingress": 2,
                "model_pending": 3,
                "cloud_review_retry": 4,
                "aggregation_waiting": 1,
            },
        }
    )

    registry.update(report)

    stored = registry.get("edge_01")["resources"]
    assert stored["cpu_measurement_status"] == "DEGRADED"
    assert stored["accelerator_measurement_status"] == "STALE"
    assert stored["queue_measurement_status"] == "FAILED"
    assert stored["queue_breakdown"] == {
        "ingress": 2,
        "model_pending": 3,
        "cloud_review_retry": 4,
        "aggregation_waiting": 1,
    }


def test_cloud_rejects_unknown_resource_measurement_status() -> None:
    registry = EdgeStatusRegistry()
    report = _report()
    report["resources"]["cpu_measurement_status"] = "SUSPICIOUS"

    with pytest.raises(EdgeStatusValidationError, match="cpu_measurement_status"):
        registry.update(report)


def test_cloud_rejects_invalid_queue_breakdown() -> None:
    registry = EdgeStatusRegistry()
    report = _report()
    report["resources"]["queue_breakdown"] = {"ingress": -1}

    with pytest.raises(EdgeStatusValidationError, match="queue_breakdown"):
        registry.update(report)


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
