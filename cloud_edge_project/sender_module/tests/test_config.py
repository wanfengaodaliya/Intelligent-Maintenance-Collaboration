"""阶段 4：Sender 配置兼容与双 Edge MQTT 代理端口映射校验。

加载真实 sender_module/config/local.json，校验三个 Sender 的六条
Sender→Edge MQTT 链路端口（*31 → edge_01，*32 → edge_02）与
network_simulator/config/entities.yaml 的链路拓扑一致。
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sender.config import ConfigError, SenderNodeConfig, load_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "local.json"

EXPECTED_PROXY_PORTS = {
    "sender_01": {"edge_01": 18831, "edge_02": 18832},
    "sender_02": {"edge_01": 18931, "edge_02": 18932},
    "sender_03": {"edge_01": 19031, "edge_02": 19032},
}


def _load_local_config():
    return load_config(CONFIG_PATH)


def test_local_config_defines_three_senders():
    config = _load_local_config()
    assert [node.sender_id for node in config.senders] == [
        "sender_01",
        "sender_02",
        "sender_03",
    ]
    assert [node.unit_id for node in config.senders] == [
        "bearing_01",
        "bearing_02",
        "bearing_03",
    ]


def _write_config(tmp_path: Path, transform) -> Path:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    transform(raw)
    path = tmp_path / "sender.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_sender_count_is_deployment_configurable(tmp_path: Path):
    def keep_one_sender(raw):
        raw["senders"] = raw["senders"][:1]

    config = load_config(_write_config(tmp_path, keep_one_sender))

    assert [node.sender_id for node in config.senders] == ["sender_01"]


def test_sender_list_cannot_be_empty(tmp_path: Path):
    def remove_senders(raw):
        raw["senders"] = []

    with pytest.raises(ConfigError, match="at least one"):
        load_config(_write_config(tmp_path, remove_senders))


def test_new_unit_id_config_keeps_bearing_compatibility_view(tmp_path: Path):
    def use_unit_id(raw):
        for sender in raw["senders"]:
            sender["unit_id"] = sender.pop("bearing_id")

    config = load_config(_write_config(tmp_path, use_unit_id))

    assert config.senders[0].unit_id == "bearing_01"
    assert config.senders[0].bearing_id == "bearing_01"
    assert "unit_id" in asdict(config.senders[0])
    assert "bearing_id" not in asdict(config.senders[0])


def test_legacy_python_constructor_accepts_bearing_id_keyword() -> None:
    node = SenderNodeConfig(
        sender_id="sender_01",
        bearing_id="bearing_01",
        scheduler_url="http://scheduler",
        mqtt_host="mqtt",
        mqtt_port=1883,
    )

    assert node.unit_id == "bearing_01"
    assert node.bearing_id == "bearing_01"


def test_conflicting_unit_and_bearing_ids_are_rejected(tmp_path: Path):
    def add_conflict(raw):
        raw["senders"][0]["unit_id"] = "bearing_99"

    with pytest.raises(ConfigError, match="unit_id and bearing_id must match"):
        load_config(_write_config(tmp_path, add_conflict))


def test_equal_unit_and_bearing_ids_are_accepted(tmp_path: Path):
    def add_equal_unit_id(raw):
        for sender in raw["senders"]:
            sender["unit_id"] = sender["bearing_id"]

    config = load_config(_write_config(tmp_path, add_equal_unit_id))

    assert config.senders[0].unit_id == "bearing_01"


def test_missing_unit_and_bearing_ids_are_rejected(tmp_path: Path):
    def remove_identity(raw):
        raw["senders"][0].pop("bearing_id")

    with pytest.raises(ConfigError, match="unit_id or bearing_id"):
        load_config(_write_config(tmp_path, remove_identity))


@pytest.mark.parametrize("field", ["unit_id", "bearing_id"])
def test_blank_identity_fields_are_rejected(tmp_path: Path, field: str):
    def blank_identity(raw):
        raw["senders"][0].pop("bearing_id")
        raw["senders"][0][field] = " "

    with pytest.raises(ConfigError, match=f"{field} must be a non-empty string"):
        load_config(_write_config(tmp_path, blank_identity))


def test_every_sender_maps_both_edge_mqtt_proxy_ports():
    config = _load_local_config()
    for node in config.senders:
        expected = EXPECTED_PROXY_PORTS[node.sender_id]
        assert dict(node.edge_mqtt_proxy_ports) == expected, node.sender_id


def test_default_mqtt_port_is_the_edge_01_proxy_port():
    config = _load_local_config()
    for node in config.senders:
        assert node.mqtt_port == EXPECTED_PROXY_PORTS[node.sender_id]["edge_01"]


def test_all_six_proxy_ports_are_distinct():
    config = _load_local_config()
    ports = [
        port
        for node in config.senders
        for port in node.edge_mqtt_proxy_ports.values()
    ]
    assert len(ports) == 6
    assert len(set(ports)) == 6
