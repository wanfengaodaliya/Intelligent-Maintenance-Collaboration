"""缓传计划 _delivery_plan 的单元测试。

覆盖三种场景：
1. 带宽充足（≥ 实时需求）时保持 realtime，间隔为采样窗口 base_interval（50ms）。
2. 带宽不足实时需求但 ≥ 4Mbps 时进入 buffered，间隔按链路有效容量（扣除丢包）推导。
3. 无链路快照时保持旧兼容行为：realtime + 50ms。

低于 4Mbps 时，_delivery_plan 也会返回可重试的 503，确保已分配任务的幂等
重试等旁路不能绕过门槛；Sender 沿用调度重试机制等待网络恢复。
"""

from types import SimpleNamespace

import pytest

from scheduler.assignment_scheduler import (
    AssignmentError,
    AssignmentScheduler,
    _delivery_plan,
)


REQUEST = {
    "packet_size_bytes": 41_900,
    "expected_packet_count": 80,
    "expected_duration_ms": 4_000,
}


def _link(mbps: float, loss: float = 0.0):
    return SimpleNamespace(
        available_throughput_mbps=mbps,
        simulated_packet_loss_rate=loss,
    )


def test_delivery_plan_keeps_realtime_when_bandwidth_is_enough():
    mode, interval, available = _delivery_plan(REQUEST, _link(10.0))
    assert (mode, interval, available) == ("realtime", 50, 10.0)


def test_delivery_plan_slows_down_at_five_mbps():
    mode, interval, available = _delivery_plan(REQUEST, _link(5.0))
    assert mode == "buffered"
    assert interval == 68
    assert available == 5.0


def test_delivery_plan_slows_down_at_four_mbps_with_loss():
    mode, interval, available = _delivery_plan(REQUEST, _link(4.0, 0.01))
    assert mode == "buffered"
    assert interval == 85
    assert available == 4.0


def test_delivery_plan_keeps_old_behavior_without_snapshot():
    assert _delivery_plan(REQUEST, None) == ("realtime", 50, None)


def test_delivery_plan_rejects_link_below_buffered_minimum():
    with pytest.raises(AssignmentError) as captured:
        _delivery_plan(REQUEST, _link(0.5))

    assert captured.value.code == "NO_AVAILABLE_EDGE_NODE"
    assert captured.value.status_code == 503
    assert captured.value.details["retryable"] is True
    assert captured.value.details["available_mbps"] == 0.5


def test_assigned_task_retry_cannot_bypass_buffered_minimum():
    registry = SimpleNamespace(link_snapshot=lambda _sender, _edge: _link(0.5))
    scheduler = AssignmentScheduler(
        registry,
        SimpleNamespace(),
        edge_client=SimpleNamespace(),
    )
    scheduler._claim_or_wait = lambda _request, _claim, _deadline: {
        "assignment_status": "ASSIGNED",
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "edge_node_id": "edge_01",
        "target_topic": "edge/edge_01/input",
    }

    with pytest.raises(AssignmentError) as captured:
        scheduler.decide(REQUEST | {
            "device_id": "machine_01",
            "sender_id": "sender_01",
            "task_id": "sd_01_tk_0001",
            "bearing_id": "bearing_01",
            "created_timestamp_ns": 1,
        })

    assert captured.value.status_code == 503
    assert captured.value.details["retryable"] is True
