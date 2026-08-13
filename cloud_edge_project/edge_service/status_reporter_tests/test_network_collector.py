from types import SimpleNamespace

from edge_status_reporter.network import SimulationNetworkCollector


def test_network_collector_maps_simulator_link_response():
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "available": True,
            "last_apply_success": True,
            "last_apply_timestamp_ns": 123,
            "applied_parameters": {
                "latency_ms": 20,
                "jitter_ms": 5,
                "bandwidth_kbps": 12_000,
                "packet_loss_percent": 1.0,
            },
        },
    )
    collector = SimulationNetworkCollector(
        "http://network/link", http_get=lambda *args, **kwargs: response
    )

    snapshot = collector.collect()

    assert snapshot.measured_at_ns == 123
    assert snapshot.available_uplink_mbps_estimate == 12.0
    assert snapshot.rtt_ms_avg == 20.0
    assert snapshot.rtt_ms_p95 == 30.0
    assert snapshot.loss_rate == 0.01


def test_network_collector_reports_unavailable_when_simulator_cannot_be_read():
    collector = SimulationNetworkCollector(
        "http://network/link",
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
        clock_ns=lambda: 456,
        monotonic=lambda: 10.0,
        stale_after_seconds=0.0,
    )

    snapshot = collector.collect()

    assert snapshot.measured_at_ns == 456
    assert snapshot.available_uplink_mbps_estimate == 0.0
    assert snapshot.loss_rate == 1.0
