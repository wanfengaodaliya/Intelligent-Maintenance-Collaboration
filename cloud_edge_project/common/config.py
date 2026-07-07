"""Small configuration loader for configs/local.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "local.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "local",
    "services": {
        "edge": {"host": "127.0.0.1", "port": 8001},
        "scheduler": {"host": "127.0.0.1", "port": 8003},
        "cloud": {"host": "127.0.0.1", "port": 8004},
        "log": {"host": "127.0.0.1", "port": 8006},
    },
    "model": {"edge_backend": "mock", "cloud_backend": "mock"},
    "log": {"path": "logs/task_trace.jsonl"},
    "demo": {
        "network_state": {
            "latency_ms": 30.0,
            "bandwidth_mbps": 20.0,
            "packet_loss": 0.01,
            "cloud_available": True,
        },
        "node_state": {
            "edge_cpu_usage": 0.55,
            "edge_memory_usage": 0.62,
            "cloud_queue_length": 3,
        },
    },
}


def _parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    """Parse the limited YAML shape used by configs/local.yaml."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, _, value = raw_line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]
        if value.strip():
            current[key] = _parse_scalar(value)
        else:
            current[key] = {}
            stack.append((indent, current[key]))
    return root


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = {key: value.copy() if isinstance(value, dict) else value for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path = CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_CONFIG
    return _deep_merge(DEFAULT_CONFIG, _parse_simple_yaml(config_path))


def service_url(service: str, config: dict[str, Any] | None = None) -> str:
    loaded = config or load_config()
    service_config = loaded["services"][service]
    return f"http://{service_config['host']}:{service_config['port']}"

