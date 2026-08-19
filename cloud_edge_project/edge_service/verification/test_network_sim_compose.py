from __future__ import annotations

import re
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[1] / "compose.network-sim.yml"

# 与 internet_service/network_simulator/config/links.yaml 对齐的链路代理端口。
EXPECTED_EDGE_ROUTES = {
    "edge_01": {
        "scheduler": "http://toxiproxy:18011",
        "cloud": "http://toxiproxy:18021",
        "link_id": "edge_01__to__scheduler__http",
    },
    "edge_02": {
        "scheduler": "http://toxiproxy:18051",
        "cloud": "http://toxiproxy:18053",
        "link_id": "edge_02__to__scheduler__http",
    },
}


def _services_section(text: str) -> str:
    section = re.search(r"^services:\n(.*?)(?=^[a-zA-Z_]+:)", text, re.M | re.S)
    assert section is not None, "compose must define a services section"
    return section.group(1)


def _service_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    matches = list(re.finditer(r"^  ([a-zA-Z0-9_-]+):\n", _services_section(text), re.M))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(_services_section(text))
        blocks[match.group(1)] = _services_section(text)[match.end():end]
    return blocks


def _environment(block: str) -> dict[str, str]:
    env: dict[str, str] = {}
    section = re.search(r"^    environment:\n((?:      .+\n)+)", block, re.M)
    assert section is not None, "each edge service must define environment variables"
    for line in section.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        match = re.match(r"      ([A-Z_]+): (.+)$", line)
        assert match is not None, f"unexpected environment line: {line}"
        env[match.group(1)] = match.group(2).strip()
    return env


def test_compose_defines_exactly_the_sim_edge_nodes() -> None:
    blocks = _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert set(blocks) == {"edge_01", "edge_02"}


def test_node_identities_topics_and_clients_are_unique() -> None:
    environments = [
        _environment(block)
        for block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).values()
    ]
    node_ids = [env["EDGE_NODE_ID"] for env in environments]
    topics = [env["EDGE_MQTT_INPUT_TOPIC"] for env in environments]
    clients = [env["EDGE_MQTT_CLIENT_ID"] for env in environments]
    assert len(set(node_ids)) == 2
    assert len(set(topics)) == 2
    assert len(set(clients)) == 2
    for node_id, topic in zip(node_ids, topics):
        assert topic == f"edge/{node_id}/input"


def test_outbound_http_goes_through_matching_toxiproxy_links() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for name, block in _service_blocks(text).items():
        env = _environment(block)
        expected = EXPECTED_EDGE_ROUTES[env["EDGE_NODE_ID"]]
        assert env["SCHEDULER_SERVICE_BASE_URL"] == expected["scheduler"], name
        assert env["CLOUD_SERVICE_BASE_URL"] == expected["cloud"], name
        assert env["EDGE_NETWORK_LINK_ID"] == expected["link_id"], name
        assert expected["link_id"] in env["EDGE_NETWORK_STATUS_URL"], name
        assert env["EDGE_MQTT_HOST"] == "mqtt-broker", name


def test_data_volumes_and_host_ports_do_not_collide() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    volumes = re.findall(r"- (edge_[a-z0-9_]+):/app/data", text)
    assert len(volumes) == 2
    assert len(set(volumes)) == 2
    host_ports: list[str] = []
    for block in _service_blocks(text).values():
        host_ports.extend(re.findall(r"- \"(\d+):\d+\"", block))
    assert len(set(host_ports)) == len(host_ports)
    assert set(host_ports) <= {"8001", "8002", "8011", "8012"}
