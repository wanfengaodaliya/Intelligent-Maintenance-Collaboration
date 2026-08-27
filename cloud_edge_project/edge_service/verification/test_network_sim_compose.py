from __future__ import annotations

import re
from pathlib import Path

EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = EDGE_SERVICE_ROOT / "compose.multi-edge.yml"
DELETED_NETWORK_SIM_COMPOSE = EDGE_SERVICE_ROOT / "compose.network-sim.yml"
MODEL_SERVICE_APP_PATH = EDGE_SERVICE_ROOT / "src" / "model_service" / "app.py"
EDGE_APP_PATH = EDGE_SERVICE_ROOT / "app.py"
START_PROJECT_PATH = EDGE_SERVICE_ROOT.parents[1] / "start_project.ps1"

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

# 正式拓扑地址在 Compose 中固定取值，不允许 ${...} 插值（.env 覆盖）。
FIXED_TOPOLOGY_ENV_VARS = (
    "EDGE_MQTT_HOST",
    "EDGE_MQTT_PORT",
    "SCHEDULER_SERVICE_BASE_URL",
    "CLOUD_SERVICE_BASE_URL",
)


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


def test_old_network_sim_compose_is_removed() -> None:
    assert not DELETED_NETWORK_SIM_COMPOSE.exists(), (
        "compose.network-sim.yml 已被 compose.multi-edge.yml 取代，不得保留兼容副本"
    )


def test_compose_defines_exactly_the_two_edge_nodes() -> None:
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


def test_edge_nodes_do_not_receive_suggestion_llm_configuration() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for name, block in _service_blocks(text).items():
        env = _environment(block)
        assert not any("SUGGESTION_LLM" in key for key in env), name


def test_both_edges_accept_deployment_timeout_overrides() -> None:
    for name, block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).items():
        env = _environment(block)
        assert env["EDGE_MODEL_QUEUE_WAIT_MS"] == "${EDGE_MODEL_QUEUE_WAIT_MS:-250}", name
        assert env["EDGE_MODEL_TOTAL_TIMEOUT_MS"] == "${EDGE_MODEL_TOTAL_TIMEOUT_MS:-2000}", name


def test_both_edges_accept_inference_worker_override() -> None:
    for name, block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).items():
        env = _environment(block)
        assert env["EDGE_MODEL_INFERENCE_WORKERS"] == "${EDGE_MODEL_INFERENCE_WORKERS:-1}", name


def test_both_edges_accept_queue_capacity_and_build_revision_overrides() -> None:
    for name, block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).items():
        env = _environment(block)
        assert env["EDGE_MODEL_QUEUE_CAPACITY"] == "${EDGE_MODEL_QUEUE_CAPACITY:-64}", name
        assert env["EDGE_BUILD_REVISION"] == "${EDGE_BUILD_REVISION:-unknown}", name


def test_both_edges_accept_expected_packet_count_override() -> None:
    for name, block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).items():
        env = _environment(block)
        assert env["EDGE_EXPECTED_PACKET_COUNT"] == "${EDGE_EXPECTED_PACKET_COUNT:-80}", name


def test_canonical_startup_pins_benchmark_runtime_and_records_it() -> None:
    text = START_PROJECT_PATH.read_text(encoding="utf-8-sig")
    assert "[int]$EdgeModelInferenceWorkers = 2" in text
    assert "[int]$EdgeModelQueueCapacity = 160" in text
    assert "[int]$EdgeModelQueueWaitMs = 15000" in text
    assert "[int]$EdgeModelTotalTimeoutMs = 20000" in text
    assert "[int]$SummaryWindowTimeoutSeconds = 40" in text
    assert "[int]$ExpectedPacketCount = 80" in text
    assert "$env:EDGE_EXPECTED_PACKET_COUNT = [string]$ExpectedPacketCount" in text
    assert "SCHEDULER_EXPECTED_PACKET_COUNT" in text
    assert '"run_config.json"' in text


def test_startup_can_skip_cloud_update_llm_without_disabling_summary_llm() -> None:
    text = START_PROJECT_PATH.read_text(encoding="utf-8-sig")
    assert "[switch]$SkipCloudUpdateLLM" in text
    assert "$summaryLlmEnabled = if ($SkipLLM)" in text
    assert "if (-not $SkipCloudUpdateLLM)" in text


def test_startup_initializes_only_the_current_edge_experiment_directory() -> None:
    text = START_PROJECT_PATH.read_text(encoding="utf-8-sig")
    assert "EDGE_EXPERIMENT_DATABASE_PATH" in text
    assert "p.mkdir(parents=True, exist_ok=True)" in text
    assert "--user 0:0" not in text
    assert "os.chown" not in text
    assert "foreach ($edgeServiceName in \"edge_01\", \"edge_02\")" in text


def test_both_edges_limit_local_h5_torch_threads_by_default() -> None:
    for name, block in _service_blocks(COMPOSE_PATH.read_text(encoding="utf-8")).items():
        env = _environment(block)
        assert env["EDGE_TORCH_INTRAOP_THREADS"] == "${EDGE_TORCH_INTRAOP_THREADS:-1}", name
        assert env["EDGE_TORCH_INTEROP_THREADS"] == "${EDGE_TORCH_INTEROP_THREADS:-1}", name


def test_topology_addresses_are_fixed_and_not_env_overridable() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for name, block in _service_blocks(text).items():
        env = _environment(block)
        for variable in FIXED_TOPOLOGY_ENV_VARS:
            value = env.get(variable)
            assert value, f"{name}: {variable} is required"
            assert "${" not in value and "}" not in value, (
                f"{name}: {variable} 必须固定取值，不允许 .env 插值覆盖"
            )


def test_compose_joins_the_external_simulator_network() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"^networks:\n  default:\n    name: \$\{NETWORK_SIM_NETWORK:-network_simulator_default\}\n"
        r"    external: true\n",
        text,
        re.M,
    ), "compose 必须以外部网络加入 network_simulator_default"


def test_data_volumes_and_host_ports_do_not_collide() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    volumes = re.findall(r"- (edge_[a-z0-9_]+):/app/data", text)
    assert len(volumes) == 2
    assert len(set(volumes)) == 2
    host_ports: list[str] = []
    for block in _service_blocks(text).values():
        host_ports.extend(re.findall(r"- \"(\d+):\d+\"", block))
    assert len(set(host_ports)) == len(host_ports)
    assert set(host_ports) <= {"8001", "8002", "8011", "8013"}


def _compose_host_ports(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    ports: set[str] = set()
    for block in _service_blocks(text).values():
        ports.update(re.findall(r"- \"(\d+):\d+\"", block))
    return ports


def test_model_service_port_is_reserved_and_never_mapped_by_compose() -> None:
    """正式模型服务统一使用 8012；任何 Compose 宿主机端口映射都不得占用它。"""
    model_app = MODEL_SERVICE_APP_PATH.read_text(encoding="utf-8")
    match = re.search(r'add_argument\("--port", type=int, default=(\d+)\)', model_app)
    assert match is not None, "model_service/app.py must define a --port default"
    model_port = match.group(1)

    edge_app = EDGE_APP_PATH.read_text(encoding="utf-8")
    client_match = re.search(
        r'EDGE_MODEL_BASE_URL", "http://127\.0\.0\.1:(\d+)"', edge_app
    )
    assert client_match is not None, "edge app.py must define EDGE_MODEL_BASE_URL default"
    assert client_match.group(1) == model_port, (
        "model_service 默认端口必须与 Edge ModelClient 默认端口一致"
    )

    host_ports = _compose_host_ports(COMPOSE_PATH)
    assert model_port not in host_ports, (
        f"{COMPOSE_PATH.name} 的宿主机端口映射占用了模型服务端口 {model_port}"
    )
