# -*- coding: utf-8 -*-
"""时间线采集器。

模式一 snap：测试期间轮询关键端点（只读），落 JSONL。
    python timeline.py snap --seconds 600 --interval-ms 500 --out ../runs/<phase>/timeline_snap.jsonl

模式二 rebuild：测试后从四层数据源重建逐窗口时间线（只读）。
    python timeline.py rebuild --sender-logs <packet_logs.jsonl> \
        --since-ns <测试开始时间> --out ../runs/<phase>/timeline_report.json

时间点定义（全部 ns 时间戳）：
    T_gen      = 窗口内最晚包 end_generate_timestamp_ns（sender 侧）
    T_edge     = Edge 首个诊断结果 edge_accepted_at_ns（bearing_decision_result）
    T_sum_ack  = summary_suggestion_outbox.acknowledged_at_ns
    T_cloud    = cloud_moment_review_record.created_at_ns（仲裁/重放）
    TTFR_edge  = T_edge - T_gen
    TTFR_cloud_selected = T_cloud - T_gen（仅链路选择样本，不计算缩减率）
    E2E        = T_sum_ack - T_gen

同窗口、无选择偏差的 Edge/Cloud TTFR 缩减率由 paired_eval.py --replay-all
计算。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

from dpb_common import (
    CLOUD_EDGE_ROOT,
    EDGE_CONTAINERS,
    SCHEDULER_URL,
    SUMMARY_URL,
    http_get_json,
    read_edge_db,
    read_cloud_db,
    read_summary_db,
    resolve_window_index,
    summarize,
)

TOOLS_DIR = Path(__file__).parent
BENCH_DIR = TOOLS_DIR.parent
DEFAULT_SENDER_LOGS = (
    CLOUD_EDGE_ROOT / "sender_module" / "runtime" / "logs" / "packet_logs.jsonl"
)


# ---------------- snap 模式 ----------------

def snap_once() -> dict:
    row: dict = {"ts_ns": time.time_ns()}
    for name, url in EDGE_CONTAINERS.items():
        try:
            h = http_get_json(f"{url}/health")
            mq = h.get("mqtt_capacity") or {}
            q = h.get("model_queue") or {}
            row[name] = {
                "mqtt_queue_depth": mq.get("queue_depth"),
                "rejected_total": mq.get("rejected_total"),
                "oversized_total": mq.get("oversized_total"),
                "queue_full_total": q.get("queue_full_total"),
                "max_observed_queued": q.get("max_observed_queued"),
                "processed": [c.get("processed") for c in q.get("consumers", [])],
                "inference_latency_ms": q.get("inference_latency_ms"),
            }
        except Exception as exc:  # noqa: BLE001
            row[name] = {"error": str(exc)}
    try:
        row["summary_metrics"] = http_get_json(f"{SUMMARY_URL}/summary/metrics")
    except Exception as exc:  # noqa: BLE001
        row["summary_metrics"] = {"error": str(exc)}
    try:
        row["scheduler"] = http_get_json(f"{SCHEDULER_URL}/health")
    except Exception as exc:  # noqa: BLE001
        row["scheduler"] = {"error": str(exc)}
    return row


def cmd_snap(args) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.seconds
    count = 0
    with out.open("w", encoding="utf-8") as fh:
        while True:
            fh.write(json.dumps(snap_once(), ensure_ascii=False) + "\n")
            fh.flush()
            count += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval_ms / 1000.0, remaining))
    print(f"saved {count} samples -> {out}")
    return 0


# ---------------- rebuild 模式 ----------------

def load_sender_logs(path: Path) -> list[dict]:
    """读取 packet_logs.jsonl，返回包级记录。"""
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_edge_results(since_ns: int, until_ns: int | None) -> list[dict]:
    """两个 Edge 容器的 revision-1 本地 bearing 结果。"""
    results = []
    for container in EDGE_CONTAINERS:
        rows = read_edge_db(
            container,
            "SELECT payload_json FROM bearing_decision_result WHERE revision=1",
        )
        for (payload_json,) in rows:
            payload = json.loads(payload_json)
            created_at_ns = int(payload.get("created_at_ns", 0))
            if created_at_ns < since_ns:
                continue
            if until_ns is not None and created_at_ns > until_ns:
                continue
            payload["_edge_container"] = container
            wstart = payload.get("window_start_sequence")
            if wstart is None:
                wstart = resolve_window_index(
                    device_id=payload.get("device_id", ""),
                    task_id=payload.get("task_id", ""),
                    bearing_id=payload.get("bearing_id", ""),
                    sender_id=payload.get("sender_id", ""),
                    diagnosis_window_id=payload.get("diagnosis_window_id", ""),
                )
            payload["_window_start_sequence"] = wstart
            results.append(payload)
    return results


def load_summary_windows(
    since_ns: int, until_ns: int | None
) -> tuple[dict, list[dict], list[dict]]:
    """Run-scoped Summary windows and suggestion acknowledgements."""
    windows = {}
    for row in read_summary_db(
        "SELECT summary_result_id, device_id, window_start_sequence, window_end_sequence, "
        "result_status, has_conflict, excluded_from_formal_metrics, payload_json, created_at_ns "
        "FROM summary_window_result "
        "WHERE created_at_ns >= ? AND (? IS NULL OR created_at_ns <= ?)",
        (since_ns, until_ns, until_ns),
    ):
        (
            sid, device_id, wstart, wend, status, has_conflict, excluded,
            payload_json, created_at_ns,
        ) = row
        windows[sid] = {
            "summary_result_id": sid,
            "device_id": device_id,
            "window_start_sequence": wstart,
            "window_end_sequence": wend,
            "result_status": status,
            "has_conflict": bool(has_conflict),
            "excluded_from_formal_metrics": bool(excluded),
            "payload": json.loads(payload_json) if payload_json else {},
            "created_at_ns": created_at_ns,
        }
    acks = []
    for row in read_summary_db(
        "SELECT summary_result_id, created_at_ns, acknowledged_at_ns, state FROM summary_suggestion_outbox"
    ):
        acks.append({
            "summary_result_id": row[0],
            "created_at_ns": row[1],
            "acknowledged_at_ns": row[2],
            "state": row[3],
        })
    arbitration_uploads = []
    for row in read_summary_db(
        "SELECT summary_result_id, state, acknowledged_at_ns "
        "FROM summary_arbitration_outbox"
    ):
        arbitration_uploads.append({
            "summary_result_id": row[0],
            "state": row[1],
            "acknowledged_at_ns": row[2],
        })
    return windows, acks, arbitration_uploads


def load_cloud_reviews(since_ns: int, until_ns: int | None) -> list[dict]:
    """cloud_moment_review_record（仲裁/重放产生的 Cloud 诊断）。"""
    rows = read_cloud_db(
        "SELECT device_id, task_id, window_start_sequence, window_end_sequence, "
        "bearing_state, confidence, model_version, created_at_ns "
        "FROM cloud_moment_review_record WHERE created_at_ns >= ? "
        "AND (? IS NULL OR created_at_ns <= ?)",
        (since_ns, until_ns, until_ns),
    )
    return [
        {
            "device_id": r[0], "task_id": r[1],
            "window_start_sequence": r[2], "window_end_sequence": r[3],
            "bearing_state": r[4], "confidence": r[5],
            "model_version": r[6], "created_at_ns": r[7],
        }
        for r in rows
    ]


def load_arbitrations(since_ns: int, until_ns: int | None) -> list[dict]:
    """device_arbitration_record（冲突解决状态）。"""
    rows = read_cloud_db(
        "SELECT arbitration_id, conflict_id, status, final_action, summary_result_id, created_at_ns "
        "FROM device_arbitration_record WHERE created_at_ns >= ? "
        "AND (? IS NULL OR created_at_ns <= ?)",
        (since_ns, until_ns, until_ns),
    )
    return [
        {
            "arbitration_id": r[0], "conflict_id": r[1], "status": r[2],
            "final_action": r[3], "summary_result_id": r[4], "created_at_ns": r[5],
        }
        for r in rows
    ]


def conflict_resolution_metrics(
    summary_windows: dict,
    suggestion_acks: list[dict],
    arbitration_uploads: list[dict],
    arbitrations: list[dict],
) -> dict:
    eligible_summary_ids = {
        sid for sid, window in summary_windows.items()
        if not window["excluded_from_formal_metrics"]
    }
    conflict_summary_ids = {
        sid for sid, window in summary_windows.items()
        if sid in eligible_summary_ids and window["has_conflict"]
    }
    arbitration_ack_ids = {
        row["summary_result_id"] for row in arbitration_uploads
        if row["state"] == "ACKNOWLEDGED"
    }
    resolved_summary_ids = {
        row["summary_result_id"] for row in arbitrations
        if row["status"] == "resolved" and row["summary_result_id"]
    }
    suggestion_ack_ids = {
        row["summary_result_id"] for row in suggestion_acks
        if row["state"] == "ACKNOWLEDGED" and row["acknowledged_at_ns"]
    }
    conflict_count = len(conflict_summary_ids)
    return {
        "metric_name": "cross-edge maintenance-action divergence",
        "eligible_windows": len(eligible_summary_ids),
        "conflict_windows": conflict_count,
        "conflict_rate": (
            round(conflict_count / len(eligible_summary_ids), 4)
            if eligible_summary_ids else None
        ),
        "arbitration_transport_acknowledged": len(
            conflict_summary_ids & arbitration_ack_ids
        ),
        "arbitration_transport_success_rate": (
            round(len(conflict_summary_ids & arbitration_ack_ids) / conflict_count, 4)
            if conflict_count else None
        ),
        "cloud_resolved": len(conflict_summary_ids & resolved_summary_ids),
        "cloud_resolution_success_rate": (
            round(len(conflict_summary_ids & resolved_summary_ids) / conflict_count, 4)
            if conflict_count else None
        ),
        "final_suggestion_acknowledged": len(
            conflict_summary_ids & suggestion_ack_ids
        ),
    }


def cmd_rebuild(args) -> int:
    since_ns = args.since_ns
    sender_records = load_sender_logs(Path(args.sender_logs))
    sender_records = [r for r in sender_records if r.get("end_generate_timestamp_ns", 0) >= since_ns]
    if args.until_ns is not None:
        sender_records = [
            r for r in sender_records
            if r.get("end_generate_timestamp_ns", 0) <= args.until_ns
        ]

    # 1) 窗口最晚生成时间：按 (device_id, window=sequence_number) 分组取 max(end_generate_timestamp_ns)
    gen_by_window: dict[tuple[str, int], int] = defaultdict(int)
    for r in sender_records:
        key = (r["device_id"], r["sequence_number"])
        gen_by_window[key] = max(gen_by_window[key], int(r["end_generate_timestamp_ns"]))

    # 2) Edge 首个诊断结果时间：按 (device_id, window) 取最早 edge_accepted_at_ns
    edge_by_window: dict[tuple[str, int], dict] = {}
    for p in load_edge_results(since_ns, args.until_ns):
        wstart = p.get("_window_start_sequence")
        if wstart is None:
            continue
        key = (p["device_id"], int(wstart))
        cur = edge_by_window.get(key)
        accepted = p.get("edge_accepted_at_ns") or p.get("created_at_ns")
        if cur is None or accepted < cur["edge_accepted_at_ns"]:
            edge_by_window[key] = {
                "edge_accepted_at_ns": accepted,
                "created_at_ns": p["created_at_ns"],
                "bearing_state": p.get("bearing_state"),
                "confidence": p.get("confidence"),
                "edge_node": p.get("_edge_container"),
                "task_id": p.get("task_id"),
            }

    # 3) Cloud 仲裁诊断（按 device+window）
    cloud_by_window: dict[tuple[str, int], int] = {}
    for r in load_cloud_reviews(since_ns, args.until_ns):
        key = (r["device_id"], r["window_start_sequence"])
        cur = cloud_by_window.get(key)
        if cur is None or r["created_at_ns"] < cur:
            cloud_by_window[key] = r["created_at_ns"]

    # 4) Summary 窗口 + ack
    summary_windows, acks, arbitration_uploads = load_summary_windows(
        since_ns, args.until_ns
    )
    ack_by_sid = {a["summary_result_id"]: a for a in acks}

    # 组装逐窗口行
    window_rows = []
    for (device_id, wseq), t_gen in sorted(gen_by_window.items()):
        row = {"device_id": device_id, "window": wseq, "t_gen_ns": t_gen}
        edge = edge_by_window.get((device_id, wseq))
        if edge:
            row["t_edge_ns"] = edge["edge_accepted_at_ns"]
            row["ttfr_edge_ms"] = round((edge["edge_accepted_at_ns"] - t_gen) / 1e6, 2)
            row["edge_state"] = edge["bearing_state"]
            row["edge_node"] = edge["edge_node"]
            row["task_id"] = edge["task_id"]
        cloud_t = cloud_by_window.get((device_id, wseq))
        if cloud_t:
            row["t_cloud_ns"] = cloud_t
            row["ttfr_cloud_ms"] = round((cloud_t - t_gen) / 1e6, 2)
        window_rows.append(row)

    # 挂 Summary 窗口信息（按 device+window）
    for sid, w in summary_windows.items():
        key = (w["device_id"], w["window_start_sequence"])
        for row in window_rows:
            if (row["device_id"], row["window"]) == key:
                row["summary_result_id"] = sid
                row["result_status"] = w["result_status"]
                row["has_conflict"] = w["has_conflict"]
                ack = ack_by_sid.get(sid)
                if ack and ack["acknowledged_at_ns"]:
                    row["t_sum_ack_ns"] = ack["acknowledged_at_ns"]
                    row["e2e_ms"] = round((ack["acknowledged_at_ns"] - row["t_gen_ns"]) / 1e6, 2)

    # 汇总
    ttfr_edge = [r["ttfr_edge_ms"] for r in window_rows if "ttfr_edge_ms" in r]
    ttfr_cloud = [r["ttfr_cloud_ms"] for r in window_rows if "ttfr_cloud_ms" in r]
    e2e_all = [r["e2e_ms"] for r in window_rows if "e2e_ms" in r]
    e2e_clean = [r["e2e_ms"] for r in window_rows if "e2e_ms" in r and not r.get("has_conflict")]
    e2e_conflict = [r["e2e_ms"] for r in window_rows if "e2e_ms" in r and r.get("has_conflict")]

    arbitrations = load_arbitrations(since_ns, args.until_ns)
    report = {
        "schema": "dual_phase_benchmark/timeline_report/v1",
        "rebuilt_at_ns": time.time_ns(),
        "since_ns": since_ns,
        "until_ns": args.until_ns,
        "window_count": len(window_rows),
        "sender_packet_count": len(sender_records),
        "summary_window_count": len(summary_windows),
        "arbitration_count": len(arbitrations),
        "conflict_resolution": conflict_resolution_metrics(
            summary_windows, acks, arbitration_uploads, arbitrations
        ),
        "ttfr": {
            "edge_ms": summarize(ttfr_edge),
            "cloud_selected_chain_ms": summarize(ttfr_cloud),
            "note": (
                "Cloud chain rows are routing-selected and must not be used for "
                "reduction; use paired_eval.py --replay-all for paired TTFR"
            ),
        },
        "e2e_ms": {
            "all": summarize(e2e_all),
            "no_conflict": summarize(e2e_clean),
            "conflict": summarize(e2e_conflict),
        },
        "windows": window_rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {k: v for k, v in report.items() if k != "windows"}, ensure_ascii=False, indent=2))
    print(f"saved -> {out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="时间线采集器（snap / rebuild）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snap", help="测试期间轮询采样")
    p_snap.add_argument("--seconds", type=float, required=True)
    p_snap.add_argument("--interval-ms", type=int, default=500)
    p_snap.add_argument("--out", type=str, required=True)
    p_snap.set_defaults(func=cmd_snap)

    p_rebuild = sub.add_parser("rebuild", help="测试后从 DB 重建时间线")
    p_rebuild.add_argument("--sender-logs", type=str, default=str(DEFAULT_SENDER_LOGS))
    p_rebuild.add_argument("--since-ns", type=int, required=True)
    p_rebuild.add_argument("--until-ns", type=int, default=None)
    p_rebuild.add_argument("--out", type=str, required=True)
    p_rebuild.set_defaults(func=cmd_rebuild)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
