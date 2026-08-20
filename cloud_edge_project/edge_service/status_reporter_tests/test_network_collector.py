from types import SimpleNamespace

import pytest

from edge_status_reporter.contracts import NetworkSnapshot
from edge_status_reporter.network import SimulationNetworkCollector


def _ok_response(timestamp_ns: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        json=lambda: {
            "available": True,
            "last_apply_success": True,
            "last_apply_timestamp_ns": timestamp_ns,
            "applied_parameters": {
                "latency_ms": 20,
                "jitter_ms": 5,
                "bandwidth_kbps": 12_000,
                "packet_loss_percent": 1.0,
            },
        },
    )


def test_network_collector_maps_simulator_link_response():
    collector = SimulationNetworkCollector(
        "http://network/link", http_get=lambda *args, **kwargs: _ok_response()
    )

    snapshot = collector.collect()

    assert snapshot.measured_at_ns == 123
    assert snapshot.available_uplink_mbps_estimate == 12.0
    assert snapshot.rtt_ms_avg == 20.0
    assert snapshot.rtt_ms_p95 == 30.0
    assert snapshot.loss_rate == 0.01
    assert snapshot.measurement_status == "OK"
    assert snapshot.last_successful_measurement_ns == 123


def test_network_collector_marks_p95_as_estimate():
    collector = SimulationNetworkCollector(
        "http://network/link", http_get=lambda *args, **kwargs: _ok_response()
    )

    snapshot = collector.collect()

    # P95 = latency + 2 * jitter 是估算值，不是实测分位数。
    assert snapshot.rtt_p95_is_estimate is True


def test_network_collector_reports_disconnected_when_link_unavailable():
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "available": False,
            "last_apply_success": True,
            "last_apply_timestamp_ns": 200,
            "applied_parameters": {
                "latency_ms": 20,
                "jitter_ms": 5,
                "bandwidth_kbps": 12_000,
                "packet_loss_percent": 0.0,
            },
        },
    )
    collector = SimulationNetworkCollector(
        "http://network/link", http_get=lambda *args, **kwargs: response
    )

    snapshot = collector.collect()

    assert snapshot.measurement_status == "DISCONNECTED"
    assert snapshot.measured_at_ns == 200
    assert snapshot.available_uplink_mbps_estimate == 0.0
    assert snapshot.loss_rate == 1.0
    assert snapshot.last_successful_measurement_ns is None


def test_network_collector_reports_failed_when_apply_not_successful():
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "available": True,
            "last_apply_success": False,
            "last_apply_timestamp_ns": 300,
        },
    )
    collector = SimulationNetworkCollector(
        "http://network/link", http_get=lambda *args, **kwargs: response
    )

    snapshot = collector.collect()

    # 链路未断开但参数未施加：数值不可信，不能冒充正常测量。
    assert snapshot.measurement_status == "FAILED"
    assert snapshot.measured_at_ns == 300
    assert snapshot.available_uplink_mbps_estimate == 0.0
    assert snapshot.loss_rate == 1.0


def test_network_collector_reports_failed_without_cache_on_collect_error():
    collector = SimulationNetworkCollector(
        "http://network/link",
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
        clock_ns=lambda: 456,
        monotonic=lambda: 10.0,
        stale_after_seconds=0.0,
    )

    snapshot = collector.collect()

    assert snapshot.measurement_status == "FAILED"
    assert snapshot.measured_at_ns == 456
    assert snapshot.available_uplink_mbps_estimate == 0.0
    assert snapshot.loss_rate == 1.0
    assert snapshot.last_successful_measurement_ns is None


def test_network_collector_serves_stale_cache_with_original_measurement_time():
    time_axis = iter([10.0, 10.5])
    collector = SimulationNetworkCollector(
        "http://network/link",
        http_get=lambda *args, **kwargs: _ok_response(),
        clock_ns=lambda: 999,
        monotonic=lambda: next(time_axis),
        stale_after_seconds=3.0,
    )
    # 第一次采集成功（monotonic=10.0）
    ok_snapshot = collector.collect()
    assert ok_snapshot.measurement_status == "OK"

    # 第二次采集失败（monotonic=10.5，缓存未过期）→ STALE
    collector.http_get = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
    stale = collector.collect()

    assert stale.measurement_status == "STALE"
    # 保留缓存原始测量时间，不伪装成新的测量。
    assert stale.measured_at_ns == 123
    assert stale.available_uplink_mbps_estimate == 12.0
    assert stale.rtt_ms_avg == 20.0
    assert stale.loss_rate == 0.01
    assert stale.last_successful_measurement_ns == 123


def test_network_collector_failed_after_success_keeps_last_ok_timestamp():
    time_axis = iter([10.0, 20.0])
    collector = SimulationNetworkCollector(
        "http://network/link",
        http_get=lambda *args, **kwargs: _ok_response(321),
        clock_ns=lambda: 789,
        monotonic=lambda: next(time_axis),
        stale_after_seconds=3.0,
    )
    ok_snapshot = collector.collect()
    assert ok_snapshot.measurement_status == "OK"
    assert ok_snapshot.measured_at_ns == 321

    # 缓存过期后采集失败 → FAILED，但保留最近一次成功测量时间。
    collector.http_get = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
    failed = collector.collect()

    assert failed.measurement_status == "FAILED"
    assert failed.measured_at_ns == 789
    assert failed.last_successful_measurement_ns == 321


def test_network_snapshot_rejects_unknown_measurement_status():
    with pytest.raises(ValueError):
        NetworkSnapshot(1, 1.0, 1.0, 1.0, 0.0, measurement_status="GREAT")


def test_network_snapshot_as_dict_includes_status_fields():
    snapshot = NetworkSnapshot(
        100, 12.0, 8.0, 10.0, 0.01,
        measurement_status="STALE",
        last_successful_measurement_ns=90,
    )

    payload = snapshot.as_dict()

    assert payload["measurement_status"] == "STALE"
    assert payload["rtt_p95_is_estimate"] is True
    assert payload["last_successful_measurement_ns"] == 90
