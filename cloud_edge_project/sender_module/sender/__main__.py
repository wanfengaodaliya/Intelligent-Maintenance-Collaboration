from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from sender.config import SenderConfig, load_config
from sender.controller import run_all_senders
from sender.scheduler_client import SchedulerClient


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


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def allocate_batch_config(config: SenderConfig) -> SenderConfig:
    scheduler = SchedulerClient(
        url=config.senders[0].scheduler_url,
        timeout_seconds=config.scheduler_timeout_seconds,
        max_retries=config.schedule_max_retries,
        retry_delay_seconds=config.deferred_retry_initial_seconds,
        retry_delay_max_seconds=config.deferred_retry_max_seconds,
        retry_jitter_ratio=config.deferred_retry_jitter_ratio,
        retry_window_seconds=config.deferred_retry_window_seconds,
    )
    return replace(
        config,
        device_id=scheduler.allocate_device_id(config.device_id),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay configured independent bearing senders")
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
    parser.add_argument(
        "--rounds",
        type=positive_int,
        default=1,
        help="run this many configured-Sender rounds",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        source_files = parse_source_files(args.source)
        summaries = []
        for _round_number in range(1, args.rounds + 1):
            # run_all_senders starts all configured Senders concurrently;
            # rounds stay sequential so one formal run has a deterministic size.
            round_config = (
                allocate_batch_config(config)
                if isinstance(config, SenderConfig)
                else config
            )
            summaries.extend(
                run_all_senders(
                    round_config,
                    source_files,
                    realtime=not args.accelerated,
                )
            )
    except Exception as exc:
        print(f"sender failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item.get("task_status") == "completed" for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
