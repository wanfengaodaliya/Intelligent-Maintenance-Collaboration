from scheduler.api import update_network_report
from scheduler.node_registry import EdgeNodeConfig, NodeRegistry


def test_simulator_report_updates_selected_formal_link_snapshot(monkeypatch):
    registry = NodeRegistry(
        {
            "edge_01": EdgeNodeConfig(
                "edge_01", "http://127.0.0.1:8001", "edge/edge_01/input"
            )
        }
    )
    monkeypatch.setattr("scheduler.api.node_registry", registry)
    payload = {
        "report_sequence": 7,
        "generated_at_ns": 100,
        "links": [
            {
                "link_id": "sender_01__to__edge_01__mqtt",
                "sender_id": "sender_01",
                "edge_id": "edge_01",
                "protocol": "mqtt",
                "latency_ms": 20,
                "jitter_ms": 5,
                "bandwidth_kbps": 12_000,
                "packet_loss_percent": 1.0,
                "available": True,
                "last_apply_success": True,
            },
            {
                "link_id": "sender_02__to__edge_01__mqtt",
                "sender_id": "sender_02",
                "edge_id": "edge_01",
                "protocol": "mqtt",
                "latency_ms": 1,
                "jitter_ms": 1,
                "bandwidth_kbps": 25_000,
                "packet_loss_percent": 0.0,
                "available": True,
                "last_apply_success": True,
            },
        ],
    }

    acknowledgement = update_network_report(
        payload, selected_link_ids={"sender_01__to__edge_01__mqtt"}
    )
    snapshot = registry.link_snapshot("sender_01", "edge_01", now_ns=1)

    assert acknowledgement == {"accepted": True, "report_sequence": 7}
    assert snapshot is not None
    assert snapshot.rtt_ms_avg == 20.0
    assert snapshot.rtt_ms_p95 == 30.0
    assert snapshot.available_throughput_mbps == 12.0
    assert snapshot.mqtt_publish_success_rate == 0.99
    assert registry.link_snapshot("sender_02", "edge_01", now_ns=1) is None


def test_disconnected_simulator_link_becomes_zero_capacity(monkeypatch):
    registry = NodeRegistry(
        {
            "edge_01": EdgeNodeConfig(
                "edge_01", "http://127.0.0.1:8001", "edge/edge_01/input"
            )
        }
    )
    monkeypatch.setattr("scheduler.api.node_registry", registry)

    update_network_report(
        {
            "report_sequence": 8,
            "generated_at_ns": 101,
            "links": [
                {
                    "link_id": "sender_01__to__edge_01__mqtt",
                    "sender_id": "sender_01",
                    "edge_id": "edge_01",
                    "protocol": "mqtt",
                    "latency_ms": None,
                    "jitter_ms": None,
                    "bandwidth_kbps": None,
                    "packet_loss_percent": 100.0,
                    "available": False,
                    "last_apply_success": True,
                }
            ],
        },
        selected_link_ids={"sender_01__to__edge_01__mqtt"},
    )

    snapshot = registry.link_snapshot("sender_01", "edge_01", now_ns=1)
    assert snapshot is not None
    assert snapshot.available_throughput_mbps == 0.0
    assert snapshot.mqtt_publish_success_rate == 0.0
