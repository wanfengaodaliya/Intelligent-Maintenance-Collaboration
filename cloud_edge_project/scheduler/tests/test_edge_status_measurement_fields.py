from scheduler.assignment_scheduler import _network_score
from scheduler.node_registry import EdgeNodeConfig, LinkSnapshot, NodeRegistry, RegistryError


def _registry() -> NodeRegistry:
    return NodeRegistry(
        {
            "edge_01": EdgeNodeConfig(
                "edge_01", "http://127.0.0.1:8001", "edge/edge_01/input"
            )
        }
    )


def _report(reported_at_ns: int, network_extra: dict | None = None) -> dict:
    network = {
        "measured_at_ns": reported_at_ns,
        "available_uplink_mbps_estimate": 12.0,
        "rtt_ms_avg": 8.0,
        "rtt_ms_p95": 10.0,
        "loss_rate": 0.01,
    }
    if network_extra is not None:
        network.update(network_extra)
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
        "network_to_scheduler": network,
        "last_task_activity_ns": 0,
    }


def test_status_report_with_measurement_status_fields_is_stored():
    registry = _registry()

    outcome = registry.update_status(
        _report(
            1,
            {
                "measurement_status": "STALE",
                "rtt_p95_is_estimate": True,
                "last_successful_measurement_ns": 90,
            },
        ),
        received_at_ns=1,
        received_monotonic_ns=1,
    )

    assert outcome["accepted"] is True
    network = registry._nodes["edge_01"].report["network_to_scheduler"]
    assert network["measurement_status"] == "STALE"
    assert network["rtt_p95_is_estimate"] is True
    assert network["last_successful_measurement_ns"] == 90


def test_status_report_without_measurement_status_fields_stays_compatible():
    registry = _registry()

    outcome = registry.update_status(
        _report(1),
        received_at_ns=1,
        received_monotonic_ns=1,
    )

    assert outcome["accepted"] is True
    network = registry._nodes["edge_01"].report["network_to_scheduler"]
    assert "measurement_status" not in network
    assert "rtt_p95_is_estimate" not in network
    assert "last_successful_measurement_ns" not in network


def test_status_report_rejects_unknown_measurement_status():
    registry = _registry()

    try:
        registry.update_status(
            _report(1, {"measurement_status": "PERFECT"}),
            received_at_ns=1,
            received_monotonic_ns=1,
        )
    except RegistryError as error:
        assert error.code == "INVALID_STATUS_REPORT"
    else:
        raise AssertionError("expected RegistryError")


def test_status_report_with_resource_measurement_status_is_stored():
    """AUD-03/04/11: cpu/accelerator/queue 采集状态必须进入 Scheduler 存储的报告。"""
    registry = _registry()
    report = _report(1)
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

    outcome = registry.update_status(
        report, received_at_ns=1, received_monotonic_ns=1
    )

    assert outcome["accepted"] is True
    resources = registry._nodes["edge_01"].report["resources"]
    assert resources["cpu_measurement_status"] == "DEGRADED"
    assert resources["accelerator_measurement_status"] == "STALE"
    assert resources["queue_measurement_status"] == "FAILED"
    assert resources["queue_breakdown"] == {
        "ingress": 2,
        "model_pending": 3,
        "cloud_review_retry": 4,
        "aggregation_waiting": 1,
    }


def test_status_report_without_resource_measurement_status_stays_compatible():
    registry = _registry()

    outcome = registry.update_status(
        _report(1), received_at_ns=1, received_monotonic_ns=1
    )

    assert outcome["accepted"] is True
    resources = registry._nodes["edge_01"].report["resources"]
    for field in (
        "cpu_measurement_status",
        "accelerator_measurement_status",
        "queue_measurement_status",
        "queue_breakdown",
    ):
        assert field not in resources


def test_status_report_rejects_unknown_resource_measurement_status():
    registry = _registry()
    report = _report(1)
    report["resources"]["cpu_measurement_status"] = "SUSPICIOUS"

    try:
        registry.update_status(
            report, received_at_ns=1, received_monotonic_ns=1
        )
    except RegistryError as error:
        assert error.code == "INVALID_STATUS_REPORT"
        assert "cpu_measurement_status" in error.message
    else:
        raise AssertionError("expected RegistryError")


def test_status_report_rejects_invalid_queue_breakdown():
    registry = _registry()
    report = _report(1)
    report["resources"]["queue_breakdown"] = {"ingress": -1}

    try:
        registry.update_status(
            report, received_at_ns=1, received_monotonic_ns=1
        )
    except RegistryError as error:
        assert error.code == "INVALID_STATUS_REPORT"
        assert "queue_breakdown" in error.message
    else:
        raise AssertionError("expected RegistryError")


def test_status_report_with_memory_measurement_status_is_stored():
    """EDGE-3: memory_measurement_status 必须进入 Scheduler 存储的报告。"""
    registry = _registry()
    report = _report(1)
    report["resources"]["memory_measurement_status"] = "DEGRADED"

    outcome = registry.update_status(
        report, received_at_ns=1, received_monotonic_ns=1
    )

    assert outcome["accepted"] is True
    resources = registry._nodes["edge_01"].report["resources"]
    assert resources["memory_measurement_status"] == "DEGRADED"


def test_status_report_without_memory_measurement_status_stays_compatible():
    """EDGE-3: 旧报告缺失 memory 字段，不写入，保持旧投影。"""
    registry = _registry()

    outcome = registry.update_status(
        _report(1), received_at_ns=1, received_monotonic_ns=1
    )

    assert outcome["accepted"] is True
    resources = registry._nodes["edge_01"].report["resources"]
    assert "memory_measurement_status" not in resources


def test_status_report_rejects_unknown_memory_measurement_status():
    """EDGE-3: 非法 memory 状态与 cpu/queue 同等拒绝。"""
    registry = _registry()
    report = _report(1)
    report["resources"]["memory_measurement_status"] = "SUSPICIOUS"

    try:
        registry.update_status(
            report, received_at_ns=1, received_monotonic_ns=1
        )
    except RegistryError as error:
        assert error.code == "INVALID_STATUS_REPORT"
        assert "memory_measurement_status" in error.message
    else:
        raise AssertionError("expected RegistryError")


def _link(loss_rate: float) -> LinkSnapshot:
    return LinkSnapshot(
        sender_id="sender_01",
        edge_node_id="edge_01",
        measured_at_ns=1,
        received_at_ns=1,
        received_monotonic_ns=1,
        rtt_ms_avg=20.0,
        rtt_ms_p95=30.0,
        jitter_ms=5.0,
        available_throughput_mbps=10.0,
        simulated_packet_loss_rate=loss_rate,
    )


def test_network_score_uses_simulated_packet_loss_rate():
    # rtt_score = 100 - 30/200*100 = 85；bandwidth_score = min(10/10,1)*100 = 100
    # loss_score = (1 - 0.03) * 100 = 97
    # total = 85*0.4 + 100*0.4 + 97*0.2 = 34 + 40 + 19.4 = 93.4
    assert _network_score(_link(0.03), 10.0) == 93.4


def test_network_score_full_loss_zeroes_loss_component():
    score = _network_score(_link(1.0), 10.0)
    # loss_score = 0；total = 34 + 40 + 0 = 74
    assert score == 74.0


def test_link_snapshot_has_no_mqtt_publish_success_rate_attribute():
    import dataclasses

    field_names = {field.name for field in dataclasses.fields(LinkSnapshot)}
    assert "mqtt_publish_success_rate" not in field_names
    assert "simulated_packet_loss_rate" in field_names
