"""弱网缓速发送的单元测试。

覆盖三部分：
1. scheduler_client.validate_assignment 对缓传协议字段的解析与校验（含旧 Scheduler 兼容回退）。
2. controller 按调度返回的 delivery_interval_ms 控制墙钟发送节奏（传感器时间戳不受影响）。
3. 任务摘要中的缓传可观测字段。
"""

from types import SimpleNamespace

import pytest

from sender.config import SenderConfig, SenderNodeConfig
from sender.controller import run_sender_task
from sender.mat_reader import SignalWindow
from sender.scheduler_client import SchedulerError, validate_assignment
from sender.source_mapping import PacketSourceMappingStore


BASE = {
    "device_id": "machine_01",
    "sender_id": "sender_01",
    "task_id": "sd_01_tk_0001",
    "bearing_id": "bearing_01",
    "target_topic": "edge/edge_01/input",
}


def _validate(payload):
    return validate_assignment(
        payload,
        expected_device_id="machine_01",
        expected_sender_id="sender_01",
        expected_task_id="sd_01_tk_0001",
        expected_bearing_id="bearing_01",
    )


# ---------- validate_assignment：缓传协议字段解析 ----------


def test_assignment_accepts_buffered_delivery_fields():
    result = _validate({
        **BASE,
        "delivery_mode": "buffered",
        "delivery_interval_ms": 85,
        "available_throughput_mbps": 4.0,
    })
    assert result.delivery_mode == "buffered"
    assert result.delivery_interval_ms == 85
    assert result.available_throughput_mbps == 4.0


def test_assignment_falls_back_for_old_scheduler():
    result = _validate(BASE)
    assert result.delivery_mode == "realtime"
    assert result.delivery_interval_ms == 50
    assert result.available_throughput_mbps is None


def test_assignment_rejects_unknown_delivery_mode():
    with pytest.raises(SchedulerError):
        _validate({**BASE, "delivery_mode": "slow", "delivery_interval_ms": 50})


def test_assignment_rejects_non_positive_interval():
    with pytest.raises(SchedulerError):
        _validate({**BASE, "delivery_mode": "realtime", "delivery_interval_ms": 0})


def test_assignment_rejects_boolean_interval():
    with pytest.raises(SchedulerError):
        _validate({**BASE, "delivery_mode": "realtime", "delivery_interval_ms": True})


def test_assignment_rejects_negative_bandwidth():
    with pytest.raises(SchedulerError):
        _validate({
            **BASE,
            "delivery_mode": "buffered",
            "delivery_interval_ms": 85,
            "available_throughput_mbps": -1.0,
        })


# ---------- controller：缓速发送节奏 ----------


def _signals():
    signals = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [1.0]}
        for name, rate in {
            "vibration": 64000,
            "phase_current_1_A": 64000,
            "phase_current_2_A": 64000,
            "shaft_speed_rpm": 4000,
            "load_torque_nm": 4000,
            "bearing_radial_load_n": 4000,
        }.items()
    }
    signals["vibration"]["unit"] = "mm/s"
    signals["phase_current_1_A"]["unit"] = "A"
    signals["phase_current_2_A"]["unit"] = "A"
    signals["bearing_module_temperature_c"] = 25.0
    return signals


class _RecordingPublisher:
    def __init__(self, **kwargs):
        self.status_counts = {}
        self.packets = []

    reconnect_count = 0
    publish_retry_total = 0

    def start(self):
        pass

    def publish(self, packet, payload, topic):
        self.packets.append(packet)
        self.status_counts = {"confirmed": len(self.packets)}

    def wait_until_settled(self, timeout):
        pass

    def stop(self):
        pass


def _build_pacing_env(tmp_path, monkeypatch, packet_count=3, delivery_interval_ms=85):
    signals = _signals()
    windows = iter(
        [
            SignalWindow(
                sequence_number=number,
                start_seconds=(number - 1) * 0.05,
                end_seconds=number * 0.05,
                start_index=(number - 1) * 3200,
                end_index=number * 3200,
                window_index=number - 1,
                data=signals,
            )
            for number in range(1, packet_count + 1)
        ]
    )
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    record = SimpleNamespace(
        source_path=source_path,
        windows=lambda **kwargs: windows,
    )
    monkeypatch.setattr("sender.controller.load_mat_record", lambda path: record)

    sleep_calls = []
    # 固定 monotonic=0，使 sleep 参数即各包相对 replay_started 的目标时刻。
    monkeypatch.setattr("sender.controller.time.monotonic", lambda: 0.0)
    monkeypatch.setattr(
        "sender.controller.time.sleep", lambda seconds: sleep_calls.append(seconds)
    )

    node = SenderNodeConfig(
        "sender_01", "bearing_01", "http://scheduler", "mqtt", 1883
    )
    config = SenderConfig(
        "machine_01", (node,), 1.0, 0, 30, 1, False, 10, 20, 0, 10,
        150, 50, packet_count, tmp_path / "logs", tmp_path / "state",
    )
    scheduler = SimpleNamespace(
        assign=lambda request: SimpleNamespace(
            target_topic="edge/edge_01/input",
            schedule_retry_count=0,
            delivery_mode="buffered",
            delivery_interval_ms=delivery_interval_ms,
            available_throughput_mbps=4.0,
        )
    )
    return config, node, source_path, scheduler, sleep_calls


def test_pacing_follows_delivery_interval_ms(tmp_path, monkeypatch):
    """缓传间隔85ms时，第1/2/3包目标时刻为0/85/170ms；时间戳仍按50ms递增。"""
    config, node, source_path, scheduler, sleep_calls = _build_pacing_env(
        tmp_path, monkeypatch
    )
    publisher = _RecordingPublisher()

    run_sender_task(
        config,
        node,
        source_path,
        realtime=True,
        scheduler=scheduler,
        publisher=publisher,
        log_sink=SimpleNamespace(write_task=lambda record: None),
        task_ids=SimpleNamespace(next_task_id=lambda: "sd_01_tk_0001"),
        source_mapping_store=PacketSourceMappingStore(
            tmp_path / "packet_sources.db"
        ),
    )

    # packet 1 目标0ms（remaining=0，不 sleep）；packet 2 目标85ms；packet 3 目标170ms。
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == pytest.approx(0.085)
    assert sleep_calls[1] == pytest.approx(0.170)
    # end_generate_timestamp_ns 仍按50ms递增，不跟随发送间隔变成85ms。
    timestamps = [p["end_generate_timestamp_ns"] for p in publisher.packets]
    assert timestamps[1] - timestamps[0] == 50_000_000
    assert timestamps[2] - timestamps[1] == 50_000_000


def test_accelerated_replay_does_not_sleep(tmp_path, monkeypatch):
    """realtime=False 时不等待，保持加速回放行为。"""
    config, node, source_path, scheduler, sleep_calls = _build_pacing_env(
        tmp_path, monkeypatch
    )
    publisher = _RecordingPublisher()

    run_sender_task(
        config,
        node,
        source_path,
        realtime=False,
        scheduler=scheduler,
        publisher=publisher,
        log_sink=SimpleNamespace(write_task=lambda record: None),
        task_ids=SimpleNamespace(next_task_id=lambda: "sd_01_tk_0001"),
        source_mapping_store=PacketSourceMappingStore(
            tmp_path / "packet_sources.db"
        ),
    )

    assert sleep_calls == []
    assert len(publisher.packets) == 3


def test_summary_records_buffered_delivery_fields(tmp_path, monkeypatch):
    """任务摘要区分 realtime 与 buffered，并显示实际发送间隔。"""
    config, node, source_path, scheduler, _ = _build_pacing_env(
        tmp_path, monkeypatch
    )
    publisher = _RecordingPublisher()
    summaries = []

    run_sender_task(
        config,
        node,
        source_path,
        realtime=True,
        scheduler=scheduler,
        publisher=publisher,
        log_sink=SimpleNamespace(write_task=lambda record: summaries.append(record)),
        task_ids=SimpleNamespace(next_task_id=lambda: "sd_01_tk_0001"),
        source_mapping_store=PacketSourceMappingStore(
            tmp_path / "packet_sources.db"
        ),
    )

    summary = summaries[-1]
    assert summary["delivery_mode"] == "buffered"
    assert summary["delivery_interval_ms"] == 85
    assert summary["available_throughput_mbps"] == 4.0
    assert summary["estimated_delivery_duration_ms"] == 85 * 3
    # replay_mode 仍由 realtime 控制，不被 delivery_mode 替换。
    assert summary["replay_mode"] == "realtime"
