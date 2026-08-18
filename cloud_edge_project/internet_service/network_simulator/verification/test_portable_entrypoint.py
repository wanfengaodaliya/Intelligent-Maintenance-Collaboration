from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import yaml

from controller.main import cli, resolve_runtime_paths


NETWORK_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_paths_accept_external_absolute_directories(tmp_path: Path) -> None:
    config_dir = tmp_path / "node-config"
    log_dir = tmp_path / "node-logs"

    project_root, resolved_config, resolved_logs = resolve_runtime_paths(
        {
            "NETWORK_SIMULATOR_ROOT": str(tmp_path / "installed-module"),
            "NETWORK_CONFIG_DIR": str(config_dir),
            "NETWORK_LOG_DIR": str(log_dir),
        }
    )

    assert project_root == (tmp_path / "installed-module").resolve()
    assert resolved_config == config_dir.resolve()
    assert resolved_logs == log_dir.resolve()


def test_cli_can_validate_external_config_without_starting_runtime() -> None:
    assert cli(["--config-dir", str(NETWORK_ROOT / "config"), "--check-config"]) == 0


def test_cli_returns_failure_for_invalid_external_config(tmp_path: Path) -> None:
    assert cli(["--config-dir", str(tmp_path), "--check-config"]) == 1


def test_source_launcher_works_outside_module_directory(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(NETWORK_ROOT / "run_network_simulator.py"),
            "--config-dir",
            str(NETWORK_ROOT / "config"),
            "--check-config",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_default_compose_uses_external_mosquitto_config() -> None:
    compose = yaml.safe_load((NETWORK_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    volume = compose["services"]["mqtt-broker"]["volumes"][0]

    assert volume["source"] == (
        "${NETWORK_MOSQUITTO_CONFIG_PATH:-"
        "${NETWORK_CONFIG_HOST_DIR:-./config}/mosquitto.conf}"
    )


def test_compose_images_match_package_version() -> None:
    with (NETWORK_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]

    expected_image = f"network-simulator:{version}"
    for filename in ("docker-compose.yml", "docker-compose.vm.yml"):
        compose = yaml.safe_load((NETWORK_ROOT / filename).read_text(encoding="utf-8"))
        images = {
            service["image"]
            for service in compose["services"].values()
            if service.get("image", "").startswith("network-simulator:")
        }
        assert images == {expected_image}
