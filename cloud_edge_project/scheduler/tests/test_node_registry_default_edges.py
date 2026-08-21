"""ENV-1: Scheduler default edge registry must match the simulator's 2-edge topology.

默认启动路径（不设置 SCHEDULER_EDGE_NODES_JSON）必须注册 edge_01 与 edge_02，
使得默认 3 sender × 2 edge 的 6 条 MQTT 链路不会被 unregistered_edge_node 拒绝。
显式环境变量仍然优先，不能被默认值污染。
"""

import json

from scheduler.api import update_network_report_batch
from scheduler.node_registry import EdgeNodeConfig, NodeRegistry, load_edge_node_configs


def _clear_env(monkeypatch):
    monkeypatch.delenv("SCHEDULER_EDGE_NODES_JSON", raising=False)


def _mqtt_link(link_id, sender_id, edge_id):
    return {
        "link_id": link_id,
        "sender_id": sender_id,
        "edge_id": edge_id,
        "protocol": "mqtt",
        "latency_ms": 20,
        "jitter_ms": 5,
        "bandwidth_kbps": 12_000,
        "packet_loss_percent": 1.0,
        "available": True,
        "last_apply_success": True,
    }


# T1: 默认 Registry 必须同时包含 edge_01 与 edge_02，
#     控制回调走 Toxiproxy 链路 18042/18052（上游为 Edge 容器端口 8001/8002）。
def test_default_registry_registers_both_edges(monkeypatch):
    _clear_env(monkeypatch)
    configs = load_edge_node_configs()
    assert set(configs) == {"edge_01", "edge_02"}
    assert configs["edge_01"].control_url == "http://127.0.0.1:18042"
    assert configs["edge_01"].target_topic == "edge/edge_01/input"
    assert configs["edge_02"].control_url == "http://127.0.0.1:18052"
    assert configs["edge_02"].target_topic == "edge/edge_02/input"


# T2: 显式环境变量仍覆盖默认值，不能因加入 edge_02 默认值而污染自定义部署。
def test_environment_variable_still_overrides_defaults(monkeypatch):
    monkeypatch.setenv(
        "SCHEDULER_EDGE_NODES_JSON",
        json.dumps(
            {
                "custom_edge": {
                    "control_url": "http://10.0.0.9:9000",
                    "target_topic": "custom/in/here",
                }
            }
        ),
    )
    configs = load_edge_node_configs()
    assert set(configs) == {"custom_edge"}
    assert configs["custom_edge"].control_url == "http://10.0.0.9:9000"
    assert configs["custom_edge"].target_topic == "custom/in/here"


# T3: 默认 6 条 sender→edge MQTT 链路全部被接受，无 unregistered_edge_node 拒绝。
def test_default_three_senders_two_edges_all_accepted(monkeypatch):
    _clear_env(monkeypatch)
    registry = NodeRegistry(load_edge_node_configs())
    monkeypatch.setattr("scheduler.api.node_registry", registry)

    links = [
        _mqtt_link(
            f"sender_0{s}__to__edge_0{e}__mqtt",
            f"sender_0{s}",
            f"edge_0{e}",
        )
        for s in (1, 2, 3)
        for e in (1, 2)
    ]

    acknowledgement = update_network_report_batch(
        {"report_sequence": 9, "generated_at_ns": 9_000, "links": links}
    )

    assert acknowledgement["accepted"] is True
    assert acknowledgement["received_count"] == 6
    assert acknowledgement["accepted_count"] == 6
    assert acknowledgement["rejected_count"] == 0
    assert acknowledgement["skipped_count"] == 0
    assert acknowledgement["results"] == []
    for s in (1, 2, 3):
        for e in (1, 2):
            assert (
                registry.link_snapshot(f"sender_0{s}", f"edge_0{e}", now_ns=1) is not None
            ), f"sender_0{s} -> edge_0{e} missing"


# T4-单位版: 默认 Registry 也用默认端口/主题构造正确的 EdgeNodeConfig。
def test_default_registry_constructs_edge_node_configs(monkeypatch):
    _clear_env(monkeypatch)
    registry = NodeRegistry(load_edge_node_configs())
    states = registry._nodes
    assert states["edge_01"].config == EdgeNodeConfig(
        "edge_01", "http://127.0.0.1:18042", "edge/edge_01/input"
    )
    assert states["edge_02"].config == EdgeNodeConfig(
        "edge_02", "http://127.0.0.1:18052", "edge/edge_02/input"
    )