from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sender.config import load_config
from sender.controller import run_task


def parse_bearing_files(entries: list[str]) -> dict[str, Path]:
    bearing_files: dict[str, Path] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError("bearing file must use bearing_id=MAT_PATH")
        bearing_id, raw_path = entry.split("=", 1)
        bearing_id = bearing_id.strip()
        raw_path = raw_path.strip()
        if not bearing_id or not raw_path:
            raise ValueError("bearing file must use bearing_id=MAT_PATH")
        if bearing_id in bearing_files:
            raise ValueError(f"duplicate bearing_id: {bearing_id}")
        bearing_files[bearing_id] = Path(raw_path)
    return bearing_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay three bearing MAT records as one MQTT task")
    parser.add_argument("--config", type=Path, default=Path("config/local.json"))
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--bearing-file",
        action="append",
        required=True,
        metavar="BEARING_ID=MAT_PATH",
        help="repeat exactly three times, once for each bearing",
    )
    parser.add_argument(
        "--accelerated",
        action="store_true",
        help="publish without the normal 50 ms pacing; use only for tests",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        bearing_files = parse_bearing_files(args.bearing_file)
        summary = run_task(
            load_config(args.config),
            args.device_id,
            bearing_files,
            realtime=not args.accelerated,
        )
    except Exception as exc:
        print(f"sender failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

