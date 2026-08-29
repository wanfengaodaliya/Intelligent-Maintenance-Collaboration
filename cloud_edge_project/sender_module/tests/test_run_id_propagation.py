"""run_id 传播链合同测试。

覆盖：
1. build_sensor_packet 携带 run_id 并在空/超长时校验。
2. run_all_senders 为同一批次的所有 Sender 生成相同且非空的 run_id。
3. run_sender_task 把 run_id 写入调度请求、每个数据包与任务摘要。
"""

from types import SimpleNamespace

import pytest

from sender.config import SenderConfig, SenderNodeConfig
from sender.controller import run_all_senders, run_sender_task
from sender.mat_reader import SignalWindow
from scenarios.bearing.ingestion.packet import (
    PacketValidationError,
    build_sensor_packet,
)


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


# ---------- build_sensor_packet 合同 ----------


def test_build_sensor_packet_carries_run_id() -> None:
    packet = build_sensor_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=_signals(),
        end_generate_timestamp_ns=1,
        run_id="run_batch01",
    )
    assert packet["run_id"] == "run_batch01"


def test_build_sensor_packet_omits_run_id_without_value() -> None:
    packet = build_sensor_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=_signals(),
        end_generate_timestamp_ns=1,
        run_id=None,
    )
    assert packet["run_id"] is None


def test_build_sensor_packet_accepts_run_id_with_fixed_batch_start() -> None:
    """同一批次开始时间派生出的 run_id 可重复：两个 sender 各建一包共享同一 run_id。"""
    base = dict(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=_signals(),
        end_generate_timestamp_ns=1,
    )
    assert (
        build_sensor_packet(**base, run_id="run_shared")["run_id"]
        == build_sensor_packet(**base, run_id="run_shared")["run_id"]
    )


@pytest.mark.parametrize("bad", ["", "   "])
def test_build_sensor_packet_rejects_blank_run_id(bad: str) -> None:
    with pytest.raises(PacketValidationError):
        build_sensor_packet(
            device_id="machine_01",
            task_id="sd_01_tk_0001",
            bearing_id="bearing_01",
            sender_id="sender_01",
            sequence_number=1,
            data=_signals(),
            end_generate_timestamp_ns=1,
            run_id=bad,
        )


# ---------- run_all_senders 批次共享 ----------


def test_run_all_senders_share_one_nonempty_run_id(tmp_path) -> None:
    captured = {}

    def fake_runner(config, node, source_path, **kwargs):
        captured[node.sender_id] = kwargs
        return {"task_status": "completed", "sender_id": node.sender_id}

    node1 = SenderNodeConfig("sender_01", "bearing_01", "http://s", "mqtt", 1883)
    node2 = SenderNodeConfig("sender_02", "bearing_02", "http://s", "mqtt", 1883)
    config = SenderConfig(
        "machine_01", (node1, node2), 1.0, 0, 30, 1, False, 10, 20, 0, 10,
        150, 50, 80, tmp_path / "logs", tmp_path / "state",
    )

    run_all_senders(
        config,
        {"sender_01": "a.mat", "sender_02": "b.mat"},
        runner=fake_runner,
    )

    run_id_1 = captured["sender_01"]["run_id"]
    run_id_2 = captured["sender_02"]["run_id"]
    assert run_id_1 and run_id_2
    assert run_id_1 == run_id_2
    assert run_id_1.startswith("run_")


# ---------- run_sender_task 全链路写入 ----------


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


def _build_env(tmp_path, monkeypatch, scheduled_run_id=None):
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    windows = iter(
        [
            SignalWindow(
                sequence_number=number,
                start_seconds=(number - 1) * 0.05,
                end_seconds=number * 0.05,
                start_index=(number - 1) * 3200,
                end_index=number * 3200,
                window_index=number - 1,
                data=_signals(),
            )
            for number in range(1, 4)
        ]
    )
    record = SimpleNamespace(
        source_path=source_path,
        windows=lambda **kwargs: windows,
    )
    monkeypatch.setattr(
        "scenarios.bearing.ingestion.provider.load_mat_record",
        lambda path: record,
    )
    monkeypatch.setattr("sender.controller.time.monotonic", lambda: 0.0)
    node = SenderNodeConfig("sender_01", "bearing_01", "http://s", "mqtt", 1883)
    config = SenderConfig(
        "machine_01", (node,), 1.0, 0, 30, 1, False, 10, 20, 0, 10,
        150, 50, 3, tmp_path / "logs", tmp_path / "state",
    )
    captured_requests = []

    def assign(request):
        captured_requests.append(request)
        return SimpleNamespace(
            target_topic="edge/edge_01/input",
            schedule_retry_count=0,
            delivery_mode="realtime",
            delivery_interval_ms=50,
            available_throughput_mbps=None,
        )

    scheduler = SimpleNamespace(assign=assign)
    return config, node, source_path, scheduler, captured_requests


def test_run_sender_task_writes_run_id_throughout(tmp_path, monkeypatch):
    config, node, source_path, scheduler, captured_requests = _build_env(
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
        source_mapping_store=SimpleNamespace(save=lambda **kwargs: None),
        run_id="run_shared",
    )

    # 调度请求带 run_id
    assert captured_requests[0]["run_id"] == "run_shared"
    # 每个包都带同一 run_id
    assert len(publisher.packets) == 3
    assert {p["run_id"] for p in publisher.packets} == {"run_shared"}
    # 任务摘要带 run_id
    assert summaries[-1]["run_id"] == "run_shared"


def test_run_sender_task_generates_run_id_when_not_injected(tmp_path, monkeypatch):
    config, node, source_path, scheduler, captured_requests = _build_env(
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
        source_mapping_store=SimpleNamespace(save=lambda **kwargs: None),
    )

    generated = captured_requests[0]["run_id"]
    assert generated
    assert generated.startswith("run_")
    assert all(p["run_id"] == generated for p in publisher.packets)