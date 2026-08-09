"""Command-line entry point for Network Simulator V3."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path

from controller.application import build_application, safe_traceback
from controller.config_loader import load_config


def main(environ: Mapping[str, str] | None = None) -> int:
    environment = dict(os.environ if environ is None else environ)
    project_root = Path(
        environment.get(
            "NETWORK_SIMULATOR_ROOT",
            str(Path(__file__).resolve().parents[1]),
        )
    )
    config_dir = Path(
        environment.get("NETWORK_CONFIG_DIR", str(project_root / "config"))
    )
    log_dir = Path(
        environment.get("NETWORK_LOG_DIR", str(project_root / "logs"))
    )
    try:
        config = load_config(config_dir, environ=environment)
        application = build_application(config, log_dir)
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(__name__).error(
            "Application bootstrap failed (%s); traceback=%s",
            type(exc).__name__,
            safe_traceback(exc),
        )
        return 1
    return application.run()


if __name__ == "__main__":
    raise SystemExit(main())
