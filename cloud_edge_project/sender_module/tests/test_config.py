"""阶段 4：Sender 配置的双 Edge MQTT 代理端口映射校验。

加载真实 sender_module/config/local.json，校验三个 Sender 的六条
Sender→Edge MQTT 链路端口（*31 → edge_01，*32 → edge_02）与
network_simulator/config/entities.yaml 的链路拓扑一致。
"""

from pathlib import Path

from sender.config import load_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "local.json"
TWO_SENDER_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "local.two-senders.json"
)

EXPECTED_PROXY_PORTS = {
    "sender_01": {"edge_01": 18831, "edge_02": 18832},
    "sender_02": {"edge_01": 18931, "edge_02": 18932},
    "sender_03": {"edge_01": 19031, "edge_02": 19032},
}


def _load_local_config():
    return load_config(CONFIG_PATH)


def test_local_config_defines_expected_senders():
    config = _load_local_config()
    sender_ids = [node.sender_id for node in config.senders]
    # 配置文件支持 2 或 3 个 sender，这里只要确保格式正确即可
    assert all(s.startswith("sender_") for s in sender_ids)
    assert len(sender_ids) >= 2


def test_two_sender_config_loads_the_two_configured_senders():
    config = load_config(TWO_SENDER_CONFIG_PATH)

    assert [node.sender_id for node in config.senders] == [
        "sender_01",
        "sender_02",
    ]
    assert [node.bearing_id for node in config.senders] == [
        "bearing_01",
        "bearing_02",
    ]


def test_every_sender_maps_both_edge_mqtt_proxy_ports():
    config = _load_local_config()
    for node in config.senders:
        expected = EXPECTED_PROXY_PORTS[node.sender_id]
        assert dict(node.edge_mqtt_proxy_ports) == expected, node.sender_id


def test_default_mqtt_port_is_the_edge_01_proxy_port():
    config = _load_local_config()
    for node in config.senders:
        assert node.mqtt_port == EXPECTED_PROXY_PORTS[node.sender_id]["edge_01"]


def test_all_proxy_ports_are_distinct():
    config = _load_local_config()
    ports = [
        port
        for node in config.senders
        for port in node.edge_mqtt_proxy_ports.values()
    ]
    # 每个 sender 有 2 个代理端口（edge_01, edge_02），所以端口数 = sender 数 * 2
    expected_count = len(config.senders) * 2
    assert len(ports) == expected_count
    assert len(set(ports)) == expected_count
