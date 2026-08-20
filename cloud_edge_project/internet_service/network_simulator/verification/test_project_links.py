from pathlib import Path
import json

from controller.config_loader import load_config


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
SIMULATOR_ROOT = CONFIG_DIR.parent
PROJECT_ROOT = SIMULATOR_ROOT.parents[1]


def test_project_links_match_confirmed_component_ports():
    config = load_config(CONFIG_DIR, environ={})
    links = {link.link_id: link for link in config.links}

    expected_http_links = {
        "sender_01__to__scheduler__http": (18031, "host.docker.internal:8003"),
        "sender_02__to__scheduler__http": (18032, "host.docker.internal:8003"),
        "sender_03__to__scheduler__http": (18033, "host.docker.internal:8003"),
        "edge_01__to__scheduler__http": (18011, "host.docker.internal:8003"),
        "scheduler__to__edge_01__http": (18042, "host.docker.internal:8001"),
        "edge_01__to__cloud__http": (18021, "host.docker.internal:8004"),
        "cloud__to__edge_01__http": (18044, "host.docker.internal:8001"),
        "cloud__to__scheduler__http": (18045, "host.docker.internal:8003"),
        "edge_02__to__scheduler__http": (18051, "host.docker.internal:8003"),
        "scheduler__to__edge_02__http": (18052, "host.docker.internal:8002"),
        "edge_02__to__cloud__http": (18053, "host.docker.internal:8004"),
        "cloud__to__edge_02__http": (18054, "host.docker.internal:8002"),
    }

    for link_id, (port, upstream) in expected_http_links.items():
        link = links[link_id]
        assert link.advertised_host == "toxiproxy"
        assert link.advertised_port == port
        assert link.listen == f"0.0.0.0:{port}"
        assert link.upstream == upstream

    assert len(config.entities.senders) == 3
    assert len(config.entities.edges) == 2
    assert len(config.links) == 18


def test_every_link_has_a_unique_proxy_endpoint():
    config = load_config(CONFIG_DIR, environ={})

    assert len({link.link_id for link in config.links}) == len(config.links)
    assert len({link.proxy_name for link in config.links}) == len(config.links)
    assert len({link.listen for link in config.links}) == len(config.links)
    assert len(
        {(link.advertised_host, link.advertised_port) for link in config.links}
    ) == len(config.links)


def test_vm_upstreams_can_be_overridden_without_changing_defaults():
    config = load_config(
        CONFIG_DIR,
        environ={
            "NETWORK_LINK_UPSTREAMS_JSON": (
                '{"scheduler__to__edge_02__http":"192.168.56.22:8001",'
                '"cloud__to__edge_02__http":"192.168.56.22:8001"}'
            )
        },
    )
    links = {link.link_id: link for link in config.links}

    assert links["scheduler__to__edge_02__http"].upstream == "192.168.56.22:8001"
    assert links["cloud__to__edge_02__http"].upstream == "192.168.56.22:8001"
    assert links["scheduler__to__edge_01__http"].upstream == "host.docker.internal:8001"


def test_edge_01_business_traffic_uses_confirmed_proxy_ports():
    compose = (SIMULATOR_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    reporter = (CONFIG_DIR / "reporter.yaml").read_text(encoding="utf-8")
    sender = json.loads(
        (PROJECT_ROOT / "sender_module" / "config" / "local.json").read_text(
            encoding="utf-8"
        )
    )

    assert '18011:18011' in compose
    assert '18021:18021' in compose
    assert '18831:18831' in compose
    assert 'host.docker.internal:${NETWORK_HOST_GATEWAY:-host-gateway}' in compose
    # AUD-02: the reporter must target the batch endpoint, not a single-link URL.
    assert "/scheduler/network-reports" in reporter
    assert "/scheduler/network-reports/sender_01__to__edge_01__mqtt" not in reporter
    assert sender["senders"][0]["mqtt_port"] == 18831
