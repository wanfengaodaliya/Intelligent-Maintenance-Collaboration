# -*- coding: utf-8 -*-
"""Build the paired-evaluation task manifest from run-scoped sender logs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_sources(values: list[str]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for value in values:
        sender_id, separator, source_file = value.partition("=")
        if not separator or not sender_id or not source_file:
            raise ValueError(f"invalid --source value: {value!r}")
        sources[sender_id] = source_file
    return sources


def build_manifest(
    sender_logs: Path,
    sources: dict[str, str],
    since_ns: int,
    until_ns: int | None,
) -> list[dict]:
    tasks: dict[tuple[str, str, str, str], dict] = {}
    with sender_logs.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            timestamp = int(record.get("end_generate_timestamp_ns", 0))
            if timestamp < since_ns or (until_ns is not None and timestamp > until_ns):
                continue
            sender_id = record["sender_id"]
            if sender_id not in sources:
                raise ValueError(f"missing --source for {sender_id}")
            key = (
                record["task_id"], sender_id, record["bearing_id"], record["device_id"]
            )
            tasks[key] = {
                "task_id": record["task_id"],
                "sender_id": sender_id,
                "bearing_id": record["bearing_id"],
                "device_id": record["device_id"],
                "source_file": sources[sender_id],
            }
    return [tasks[key] for key in sorted(tasks)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a run-scoped task manifest")
    parser.add_argument("--sender-logs", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--since-ns", type=int, required=True)
    parser.add_argument("--until-ns", type=int, default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    manifest = build_manifest(
        Path(args.sender_logs),
        parse_sources(args.source),
        args.since_ns,
        args.until_ns,
    )
    if not manifest:
        raise RuntimeError("no sender tasks found in the requested run interval")
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {len(manifest)} tasks -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
