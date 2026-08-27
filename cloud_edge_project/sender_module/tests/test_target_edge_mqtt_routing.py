"""阶段 4：Scheduler 目标 Topic → Sender MQTT 代理端口分流校验。

使用 fake/mock Scheduler（返回 edge/edge_01/input 或 edge/edge_02/input，
不连接真实 Scheduler）验证：
  - Scheduler 分配 edge_01 时，三个 Sender 分别选择 18831/18931/19031；
  - Scheduler 分配 edge_02 时，三个 Sender 分别选择 18832/18932/19032；
  - 任务摘要正确记录 target_edge_node_id。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sender.config import load_config
from sender.controller import (
    _mqtt_port_for_target,
    resolve_target_edge_node_id,
    run_sender_task,
)
from sender.mat_reader import SignalWindow
from sender.source_mapping import PacketSourceMappingStore

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "local.json"

EXPECTED_PROXY_PORTS = {
    "sender_01": {"edge_01": 18831, "edge_02": 18832},
    "sender_02": {"edge_01": 18931, "edge_02": 18932},
    "sender_03": {"edge_01": 19031, "edge_02": 19032},
}

def _local_config():
    return load_config(CONFIG_PATH)


def _available_senders():
    """从配置文件动态获取可用的 sender 列表。"""
    config = _local_config()
    return tuple(node.sender_id for node in config.senders)


EDGE_TOPICS = (("edge_01", "edge/edge_01/input"), ("edge_02", "edge/edge_02/input"))


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


def _run_task(node, config, target_topic, tmp_path, monkeypatch):
    """以 mock Scheduler + 录制型 Publisher 运行一次任务，返回发布器与摘要。"""

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
            for number in range(1, config.expected_packet_count + 1)
        ]
    )
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    record = SimpleNamespace(
        source_path=source_path,
        windows=lambda **kwargs: windows,
    )
    monkeypatch.setattr("sender.controller.load_mat_record", lambda path: record)

    created = []

    class RecordingPublisher:
        reconnect_count = 0
        publish_retry_total = 0

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.status_counts = {}
            created.append(self)

        def start(self):
            pass

        def publish(self, packet, payload, topic):
            confirmed = self.status_counts.get("confirmed", 0)
            self.status_counts = {"confirmed": confirmed + 1}

        def wait_until_settled(self, timeout):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("sender.controller.MqttPublisher", RecordingPublisher)

    scheduler = SimpleNamespace(
        assign=lambda request: SimpleNamespace(
            target_topic=target_topic,
            schedule_retry_count=0,
            delivery_mode="realtime",
            delivery_interval_ms=50,
            available_throughput_mbps=None,
        )
    )
    task_id = "sd_%s_tk_0001" % node.sender_id[-2:]

    summary = run_sender_task(
        config,
        node,
        source_path,
        realtime=False,
        scheduler=scheduler,
        log_sink=SimpleNamespace(write_task=lambda record: None),
        task_ids=SimpleNamespace(next_task_id=lambda: task_id),
        source_mapping_store=PacketSourceMappingStore(
            tmp_path / "packet_sources.db"
        ),
    )
    assert len(created) == 1
    return created[0], summary


def test_resolve_target_edge_node_id_parses_edge_topics():
    assert resolve_target_edge_node_id("edge/edge_01/input") == "edge_01"
    assert resolve_target_edge_node_id("edge/edge_02/input") == "edge_02"
    assert resolve_target_edge_node_id("summary/device-results") is None
    assert resolve_target_edge_node_id("edge/edge_01/other") is None


@pytest.mark.parametrize("sender_id", _available_senders())
@pytest.mark.parametrize("edge_id,topic", EDGE_TOPICS)
def test_proxy_port_resolution_uses_local_config(sender_id, edge_id, topic):
    config = _local_config()
    node = next(n for n in config.senders if n.sender_id == sender_id)
    port, target_edge = _mqtt_port_for_target(node, topic)
    assert port == EXPECTED_PROXY_PORTS[sender_id][edge_id]
    assert target_edge == edge_id


def test_unknown_edge_target_falls_back_to_default_port():
    config = _local_config()
    node = config.senders[0]
    port, target_edge = _mqtt_port_for_target(node, "edge/edge_99/input")
    assert port == node.mqtt_port
    assert target_edge == "edge_99"


@pytest.mark.parametrize("sender_id", _available_senders())
@pytest.mark.parametrize("edge_id,topic", EDGE_TOPICS)
def test_scheduler_assignment_routes_task_to_matching_proxy_port(
    sender_id, edge_id, topic, tmp_path, monkeypatch
):
    config = _local_config()
    node = next(n for n in config.senders if n.sender_id == sender_id)

    publisher, summary = _run_task(node, config, topic, tmp_path, monkeypatch)

    assert publisher.kwargs["port"] == EXPECTED_PROXY_PORTS[sender_id][edge_id]
    assert publisher.kwargs["host"] == node.mqtt_host
    assert summary["target_topic"] == topic
    assert summary["target_edge_node_id"] == edge_id
    assert summary["task_status"] == "completed"
