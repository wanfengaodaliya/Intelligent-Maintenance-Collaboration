"""Start all first-stage FastAPI services."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from common.config import load_config


@dataclass(frozen=True)
class Service:
    name: str
    module: str
    config_key: str


SERVICES = [
    Service("edge_service", "edge_service.app:app", "edge"),
    Service("scheduler_service", "scheduler.api:app", "scheduler"),
    Service("cloud_service", "cloud_service.app:app", "cloud"),
    Service("log_service", "log_service.app:app", "log"),
]


def start_service(service: Service, config: dict[str, Any]) -> subprocess.Popen:
    service_config = config["services"][service.config_key]
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        service.module,
        "--host",
        str(service_config["host"]),
        "--port",
        str(service_config["port"]),
    ]
    print(f"starting {service.name} at http://{service_config['host']}:{service_config['port']}")
    return subprocess.Popen(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start edge, scheduler, cloud, and log services.")
    parser.add_argument("--service", choices=[service.name for service in SERVICES], help="start one service only")
    args = parser.parse_args()
    config = load_config()
    selected = [service for service in SERVICES if args.service in (None, service.name)]
    processes = [start_service(service, config) for service in selected]
    try:
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        print("stopping services")
        for process in processes:
            process.terminate()


if __name__ == "__main__":
    main()
