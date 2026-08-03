from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sender.config import load_config
from sender.controller import run_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay one MAT record as an MQTT task")
    parser.add_argument("--config", type=Path, default=Path("config/local.json"))
    parser.add_argument("--mat-file", type=Path, required=True)
    parser.add_argument(
        "--accelerated",
        action="store_true",
        help="publish without the normal 50 ms pacing; use only for tests",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        summary = run_task(
            load_config(args.config),
            args.mat_file,
            realtime=not args.accelerated,
        )
    except Exception as exc:
        print(f"sender failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

