"""Command-line entry point for Network Simulator V3."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import logging
import os
from pathlib import Path
from typing import Sequence

from controller.application import build_application, safe_traceback
from controller.config_loader import load_config


def _report_bootstrap_failure(exc: Exception) -> int:
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger(__name__).error(
        "Application bootstrap failed (%s); traceback=%s",
        type(exc).__name__,
        safe_traceback(exc),
    )
    return 1


def resolve_runtime_paths(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    environment = dict(os.environ if environ is None else environ)
    project_root = Path(
        environment.get(
            "NETWORK_SIMULATOR_ROOT",
            str(Path(__file__).resolve().parents[1]),
        )
    ).expanduser().resolve()
    config_dir = Path(
        environment.get("NETWORK_CONFIG_DIR", str(project_root / "config"))
    ).expanduser().resolve()
    log_dir = Path(
        environment.get("NETWORK_LOG_DIR", str(project_root / "logs"))
    ).expanduser().resolve()
    return project_root, config_dir, log_dir


def main(environ: Mapping[str, str] | None = None) -> int:
    environment = dict(os.environ if environ is None else environ)
    _, config_dir, log_dir = resolve_runtime_paths(environment)
    try:
        config = load_config(config_dir, environ=environment)
        application = build_application(config, log_dir)
    except Exception as exc:
        return _report_bootstrap_failure(exc)
    return application.run()


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portable network simulator launcher")
    parser.add_argument("--config-dir", help="External configuration directory")
    parser.add_argument("--log-dir", help="Writable runtime log directory")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting the simulator",
    )
    arguments = parser.parse_args(argv)
    environment = dict(os.environ)
    if arguments.config_dir:
        environment["NETWORK_CONFIG_DIR"] = arguments.config_dir
    if arguments.log_dir:
        environment["NETWORK_LOG_DIR"] = arguments.log_dir
    if arguments.check_config:
        _, config_dir, _ = resolve_runtime_paths(environment)
        try:
            load_config(config_dir, environ=environment)
        except Exception as exc:
            return _report_bootstrap_failure(exc)
        return 0
    return main(environment)


if __name__ == "__main__":
    raise SystemExit(cli())
