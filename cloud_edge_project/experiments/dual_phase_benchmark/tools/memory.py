# -*- coding: utf-8 -*-
"""内存采集器：两个 Edge 容器的 cgroup v2 memory.current 采样。

用法：
    python memory.py baseline [--seconds 30] [--interval-ms 500] [--out baseline.json]
        空闲基线采样，输出两个容器各自的 median/mean/max/min。

    python memory.py record --seconds N [--interval-ms 500] [--out series.jsonl]
        测试期间时间序列采样，每行 {ts_ns, edge_01_memory_bytes, edge_02_memory_bytes}。

只读：仅 docker exec cat /sys/fs/cgroup/memory.current。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from dpb_common import read_edge_memory_bytes, summarize

CONTAINERS = ["edge_01", "edge_02"]


def sample_once() -> dict:
    row = {"ts_ns": time.time_ns()}
    for c in CONTAINERS:
        row[f"{c}_memory_bytes"] = read_edge_memory_bytes(c)
    return row


def collect(duration_s: float, interval_ms: int, out_path: Path | None) -> list[dict]:
    rows: list[dict] = []
    deadline = time.monotonic() + duration_s
    fh = None
    if out_path:
        fh = out_path.open("w", encoding="utf-8")
    try:
        while True:
            row = sample_once()
            rows.append(row)
            if fh:
                fh.write(json.dumps(row) + "\n")
                fh.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_ms / 1000.0, remaining))
    finally:
        if fh:
            fh.close()
    return rows


def stats_of(rows: list[dict]) -> dict:
    result = {}
    for c in CONTAINERS:
        key = f"{c}_memory_bytes"
        values = [float(r[key]) for r in rows]
        result[c] = summarize(values)
        if values:
            result[c]["peak_delta_from_median"] = round(max(values) - statistics.median(values), 0)
    return result


def cmd_baseline(args) -> int:
    rows = collect(args.seconds, args.interval_ms, None)
    stats = stats_of(rows)
    report = {
        "mode": "baseline",
        "duration_s": args.seconds,
        "interval_ms": args.interval_ms,
        "sample_count": len(rows),
        "stats": stats,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved -> {args.out}")
    return 0


def cmd_record(args) -> int:
    rows = collect(args.seconds, args.interval_ms, Path(args.out))
    stats = stats_of(rows)
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        baseline_stats = baseline.get("stats") or baseline["memory_idle_baseline"]["stats"]
        for container in CONTAINERS:
            idle_p50 = baseline_stats[container]["p50"]
            run_peak = stats[container]["max"]
            stats[container]["idle_baseline_p50"] = idle_p50
            stats[container]["peak_delta_from_idle_baseline"] = round(
                run_peak - idle_p50, 0
            )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.report_out:
        Path(args.report_out).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved report -> {args.report_out}")
    print(f"saved {len(rows)} samples -> {args.out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Edge 容器 cgroup 内存采集器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_base = sub.add_parser("baseline", help="空闲基线采样")
    p_base.add_argument("--seconds", type=float, default=30.0)
    p_base.add_argument("--interval-ms", type=int, default=500)
    p_base.add_argument("--out", type=str, default=None)
    p_base.set_defaults(func=cmd_baseline)

    p_rec = sub.add_parser("record", help="测试期间时间序列采样")
    p_rec.add_argument("--seconds", type=float, required=True)
    p_rec.add_argument("--interval-ms", type=int, default=500)
    p_rec.add_argument("--out", type=str, required=True)
    p_rec.add_argument("--baseline", type=str, default=None)
    p_rec.add_argument("--report-out", type=str, default=None)
    p_rec.set_defaults(func=cmd_record)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
