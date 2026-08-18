from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sender.config import load_config
from sender.controller import run_all_senders


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "local.json"


def parse_source_files(entries: list[str]) -> dict[str, Path]:
    source_files: dict[str, Path] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError("source must use sender_id=MAT_PATH")
        sender_id, raw_path = (part.strip() for part in entry.split("=", 1))
        if not sender_id or not raw_path:
            raise ValueError("source must use sender_id=MAT_PATH")
        if sender_id in source_files:
            raise ValueError(f"duplicate sender_id: {sender_id}")
        source_files[sender_id] = Path(raw_path)
    return source_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay three independent bearing senders")
    parser.add_argument("--config", type=Path, default=_default_config_path())
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="SENDER_ID=MAT_PATH",
        help="repeat once for each configured sender",
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
        summaries = run_all_senders(
            load_config(args.config),
            parse_source_files(args.source),
            realtime=not args.accelerated,
        )
    except Exception as exc:
        print(f"sender failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item.get("task_status") == "completed" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
