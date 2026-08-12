from __future__ import annotations

import pytest

from edge_runtime.config import EdgeRuntimeConfig
from cloud_review import load_cloud_review_config
from edge_service.model import load_edge_node_id


def test_edge_node_id_keeps_existing_default() -> None:
    assert load_edge_node_id({}) == "edge_01"


def test_edge_node_id_can_be_overridden_for_virtual_machine() -> None:
    assert load_edge_node_id({"EDGE_NODE_ID": "edge_02"}) == "edge_02"


def test_edge_node_id_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="EDGE_NODE_ID"):
        load_edge_node_id({"EDGE_NODE_ID": "edge-2"})


def test_edge_runtime_environment_keeps_existing_defaults() -> None:
    config = EdgeRuntimeConfig.from_env({})

    assert config.edge_node_id == "edge_01"
    assert config.mqtt.host == "127.0.0.1"
    assert config.mqtt.port == 1883
    assert config.mqtt.input_topic == "edge/edge_01/input"
    assert config.mqtt.client_id == "edge_01-runtime"
    assert config.scheduler.base_url == "http://127.0.0.1:8003"
    assert config.control.port == 8011
    assert config.validate() == []


def test_edge_runtime_environment_builds_node_specific_defaults() -> None:
    config = EdgeRuntimeConfig.from_env(
        {
            "EDGE_NODE_ID": "edge_02",
            "EDGE_MQTT_HOST": "192.168.56.12",
            "EDGE_MQTT_PORT": "18832",
            "SCHEDULER_SERVICE_BASE_URL": "http://192.168.56.10:8003/",
            "EDGE_CONTROL_PORT": "8012",
            "EDGE_CLOUD_NODE_URLS_JSON": '{"cloud_01":"http://192.168.56.11:8004"}',
        }
    )

    assert config.edge_node_id == "edge_02"
    assert config.mqtt.host == "192.168.56.12"
    assert config.mqtt.port == 18832
    assert config.mqtt.input_topic == "edge/edge_02/input"
    assert config.mqtt.client_id == "edge_02-runtime"
    assert config.scheduler.base_url == "http://192.168.56.10:8003"
    assert config.control.port == 8012
    assert config.cloud_node_urls == {
        "cloud_01": "http://192.168.56.11:8004"
    }
    assert config.validate() == []


def test_cloud_review_outbound_urls_support_virtual_machines(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(
        "SCHEDULER_SERVICE_BASE_URL",
        "http://192.168.56.10:8003/",
    )
    monkeypatch.setenv(
        "CLOUD_SERVICE_BASE_URL",
        "http://192.168.56.11:8004/",
    )
    monkeypatch.setenv("EDGE_CLOUD_REVIEW_CACHE_DIR", str(tmp_path))

    config = load_cloud_review_config()

    assert config.scheduler_base_url == "http://192.168.56.10:8003"
    assert config.cloud_base_url == "http://192.168.56.11:8004"
    assert config.cache_directory == tmp_path
