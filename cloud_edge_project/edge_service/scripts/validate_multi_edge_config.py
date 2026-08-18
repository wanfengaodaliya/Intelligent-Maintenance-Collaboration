"""Pre-flight validation for the multi-edge compose deployment."""
# 该脚本在启动前检查 compose.multi-edge.yml：重复节点 ID、Topic、客户端 ID、
# 端口和数据卷都会直接报错，并用 EdgeRuntimeConfig.validate 复核每个节点配置。

from __future__ import annotations

import re
import sys
from pathlib import Path

EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EDGE_SERVICE_ROOT.parent
for import_root in (PROJECT_ROOT, EDGE_SERVICE_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

COMPOSE_PATH = EDGE_SERVICE_ROOT / "compose.multi-edge.yml"
_NODE_ID_PATTERN = re.compile(r"^edge_\d{2,}$")
_ENV_DEFAULT_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


def _resolve_env_value(value: str) -> str:
    """把 compose 的 ${VAR:-default} 插值解析为默认值。"""
    return _ENV_DEFAULT_PATTERN.sub(lambda match: match.group(2), str(value))


def _load_compose(path: Path) -> dict:
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except ImportError:
        return _parse_minimal_compose(path)


def _parse_minimal_compose(path: Path) -> dict:
    """无 PyYAML 时的最小解析：只覆盖本 compose 文件的固定结构。"""
    services: dict[str, dict] = {}
    current_service: str | None = None
    section: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent == 0:
                current_service, section = None, None
                continue
            if stripped.endswith(":") and indent == 2:
                current_service = stripped[:-1].strip()
                services[current_service] = {"environment": {}, "ports": [], "volumes": []}
                section = None
                continue
            if current_service is None:
                continue
            if stripped.endswith(":") and indent == 4:
                section = stripped[:-1].strip()
                continue
            if section == "environment" and indent == 6 and ":" in stripped:
                key, _, value = stripped.partition(":")
                services[current_service]["environment"][key.strip()] = value.strip()
            elif section in {"ports", "volumes"} and stripped.startswith("- "):
                services[current_service][section].append(stripped[2:].strip().strip('"'))
    return {"services": services}


def _extract_nodes(compose: dict) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    for service_name, service in (compose.get("services") or {}).items():
        env = {
            str(key): _resolve_env_value(str(value))
            for key, value in (service.get("environment") or {}).items()
        }
        node_id = env.get("EDGE_NODE_ID", service_name)
        host_ports = []
        for mapping in service.get("ports") or []:
            host_part = str(mapping).split(":")[0].strip().strip('"')
            host_ports.append(host_part)
        nodes[node_id] = {
            "service": service_name,
            "env": env,
            "client_id": env.get("EDGE_MQTT_CLIENT_ID", ""),
            "input_topic": env.get("EDGE_MQTT_INPUT_TOPIC", ""),
            "host_ports": host_ports,
            "volumes": list(service.get("volumes") or ()),
        }
    return nodes


def validate(path: Path = COMPOSE_PATH) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"compose file not found: {path}"]
    compose = _load_compose(path)
    nodes = _extract_nodes(compose)
    if len(nodes) < 2:
        errors.append("multi-edge compose must define at least two edge nodes")

    seen_client_ids: dict[str, str] = {}
    seen_topics: dict[str, str] = {}
    seen_ports: dict[str, str] = {}
    seen_volumes: dict[str, str] = {}
    for node_id, info in nodes.items():
        service = info["service"]
        if not _NODE_ID_PATTERN.fullmatch(node_id):
            errors.append(f"{service}: EDGE_NODE_ID must match edge_<at least two digits>")
        if not info["client_id"]:
            errors.append(f"{service}: EDGE_MQTT_CLIENT_ID is required")
        elif info["client_id"] in seen_client_ids:
            errors.append(
                f"{service}: MQTT client ID {info['client_id']} already used by {seen_client_ids[info['client_id']]}"
            )
        else:
            seen_client_ids[info["client_id"]] = service
        if not info["input_topic"]:
            errors.append(f"{service}: EDGE_MQTT_INPUT_TOPIC is required")
        else:
            if info["input_topic"] != f"edge/{node_id}/input":
                errors.append(
                    f"{service}: input topic must be edge/{node_id}/input, got {info['input_topic']}"
                )
            if info["input_topic"] in seen_topics:
                errors.append(
                    f"{service}: input topic {info['input_topic']} already used by {seen_topics[info['input_topic']]}"
                )
            else:
                seen_topics[info["input_topic"]] = service
        for port in info["host_ports"]:
            if port in seen_ports:
                errors.append(f"{service}: host port {port} already used by {seen_ports[port]}")
            else:
                seen_ports[port] = service
        for volume in info["volumes"]:
            source = volume.split(":")[0].strip()
            if source in seen_volumes:
                errors.append(
                    f"{service}: data volume {source} already used by {seen_volumes[source]}"
                )
            else:
                seen_volumes[source] = service
        # 用运行时自己的校验复核每个节点的完整配置。
        try:
            from edge_runtime.config import EdgeRuntimeConfig

            config_errors = EdgeRuntimeConfig.from_env(environ=info["env"]).validate()
            errors.extend(f"{service}: {item}" for item in config_errors)
        except Exception as exc:
            errors.append(f"{service}: runtime config failed to load: {exc}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("multi-edge configuration is INVALID:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("multi-edge configuration is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
