"""Operator entry point for handing a prepared update to an offline trainer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cloud_service.model_update.service import ModelUpdateService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark an update as training and print its frozen dataset manifest."
    )
    parser.add_argument("--update-task", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=None)
    arguments = parser.parse_args()

    service = ModelUpdateService(
        arguments.database, data_root=arguments.artifact_root
    )
    update = service.start_training(arguments.update_task)
    manifest = service.dataset_repository.get_by_update(arguments.update_task)
    print(
        json.dumps(
            {"update": update, "dataset_manifest": manifest},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
