# -*- coding: utf-8 -*-
"""KI08+KB23 双 Edge 极端等级分歧冲突率专项测试。

前置：
    全栈已按双发送端模式重启（新 EXPERIMENT_ID，双轴承窗口），且
    SUMMARY_CLOUD_ARBITRATION_URL 指向本机无监听端口 —— 冲突仲裁请求
    不出本地，只测冲突率、不测冲突解决。

度量（新规则）：
    冲突 = 同一设备窗口内，两个不同 Edge 对各自轴承给出的
    action_level（action_scorer_v1 0~3 等级）跨度 >= 3，
    即 payload.max_action_level_gap >= 3。
    冲突率 = 冲突窗口数 / 完整窗口数（excluded_from_formal_metrics=0）。
    旧二值状态分歧（node_states 不一致）仅作为对照输出。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(r"d:\Projects\Intelligent-Maintenance-Collaboration-clean")
CLOUD_EDGE = ROOT / "cloud_edge_project"
SENDER_MODULE = CLOUD_EDGE / "sender_module"
RUNS = CLOUD_EDGE / "experiments" / "dual_phase_benchmark" / "runs"
TOOLS = CLOUD_EDGE / "experiments" / "dual_phase_benchmark" / "tools"

SOURCES = {
    "sender_01": r"D:\Projects\test_data\KI08\N09_M07_F10_KI08_1.mat",
    "sender_02": r"D:\Projects\test_data\KB23\N09_M07_F10_KB23_1.mat",
}

LEVEL_CONFLICT_THRESHOLD = 3

EXPECTED_PACKETS = 80
EXPECTED_BEARINGS = 160
EXPECTED_WINDOWS = 80


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_experiment(max_age_s: float = 21600) -> Path:
    base = CLOUD_EDGE / "data" / "experiments"
    now = time.time()
    for d in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
        if d.is_dir() and (d / "run_config.json").exists() and (d / "summary_service.db").exists():
            if now - (d / "run_config.json").stat().st_mtime < max_age_s:
                return d
    raise SystemExit("no fresh experiment dir found under data/experiments")


def q(db: Path, sql: str, params=()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def main() -> int:
    exp = find_experiment()
    exp_id = exp.name
    sdb = exp / "summary_service.db"
    os.environ["DPB_EXPERIMENT_ID"] = exp_id
    sys.path.insert(0, str(TOOLS))
    from dpb_common import read_edge_db

    out_dir = RUNS / f"conflict_ki08_kb23_gap3_{exp_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"experiment={exp_id}")

    t0 = time.time_ns()
    (out_dir / "t0.txt").write_text(str(t0), encoding="utf-8")

    cmd = [sys.executable, "-m", "sender", "--config", "config/local.json"]
    for sid, path in SOURCES.items():
        cmd += ["--source", f"{sid}={path}"]
    log("launching sender: " + " ".join(cmd))
    sender_env = dict(os.environ)
    sender_env["PYTHONPATH"] = str(CLOUD_EDGE)
    sender = subprocess.run(
        cmd, cwd=str(SENDER_MODULE), capture_output=True, text=True,
        timeout=900, env=sender_env,
    )
    (out_dir / "sender_output.json").write_text(
        json.dumps({"returncode": sender.returncode, "stdout": sender.stdout,
                    "stderr": sender.stderr[-4000:]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log(f"sender finished rc={sender.returncode}")

    # 轮询直至 160 轴承结果 + 80 窗口全部关闭
    deadline = time.time() + 600
    stable = 0
    last_snap = None
    while time.time() < deadline:
        bears = q(sdb, "SELECT COUNT(*) FROM summary_bearing_result")[0][0]
        status = dict(q(sdb, "SELECT result_status, COUNT(*) FROM summary_window_result GROUP BY result_status"))
        total_w = sum(status.values())
        snap = (bears, total_w)
        if snap == (EXPECTED_BEARINGS, EXPECTED_WINDOWS):
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        if snap != last_snap:
            log(f"progress: bearings={bears} windows={total_w} status={status}")
            last_snap = snap
        time.sleep(2)

    t_end = time.time_ns()

    # ---------- 冲突率主指标（新规则：跨 Edge action_level 跨度 >= 3） ----------
    windows = q(
        sdb,
        "SELECT summary_result_id, result_status, has_conflict, excluded_from_formal_metrics, payload_json "
        "FROM summary_window_result",
    )
    total = len(windows)
    excluded = sum(1 for w in windows if w[3])
    eligible = [w for w in windows if not w[3]]
    incomplete = [w for w in windows if w[1] == "INCOMPLETE"]

    level_gap_dist: Counter = Counter()
    level_by_edge: dict[str, Counter] = {}
    state_by_edge: dict[str, Counter] = {}
    legacy_binary_conflicts = 0
    missing_gap = 0
    conflicts = []
    conflict_details = []
    for w in eligible:
        payload = json.loads(w[4])
        gap_raw = payload.get("max_action_level_gap")
        if gap_raw is None:
            missing_gap += 1
            gap = -1
        else:
            gap = int(gap_raw)
        level_gap_dist[gap] += 1
        for edge, level in (payload.get("action_levels_by_edge") or {}).items():
            level_by_edge.setdefault(edge, Counter())[int(level)] += 1
        for edge, state in (payload.get("node_states") or {}).items():
            state_by_edge.setdefault(edge, Counter())[state] += 1
        if payload.get("state_mismatch") is True:
            legacy_binary_conflicts += 1
        if gap >= LEVEL_CONFLICT_THRESHOLD:
            conflicts.append(w)
            conflict_details.append({
                "summary_result_id": w[0],
                "window_start": payload.get("window_start_sequence"),
                "max_action_level_gap": gap,
                "max_action_score_gap": payload.get("max_action_score_gap"),
                "action_levels_by_edge": payload.get("action_levels_by_edge"),
                "action_scores_by_edge": payload.get("action_scores_by_edge"),
                "node_states": payload.get("node_states"),
                "state_mismatch": payload.get("state_mismatch"),
                "conflict_semantics": payload.get("conflict_semantics"),
                "final_action_level": payload.get("final_action_level"),
                "source_labels": {
                    r.get("bearing_id"): r.get("diagnosis_label")
                    for r in payload.get("source_results", [])
                },
            })
    conflict_rate = (len(conflicts) / len(eligible)) if eligible else None

    # ---------- 仲裁不出本地核验 ----------
    arb = q(
        sdb,
        "SELECT state, COUNT(*), MIN(attempts), MAX(attempts) FROM summary_arbitration_outbox GROUP BY state",
    )
    arb_errors = Counter()
    for (err,) in q(
        sdb,
        "SELECT last_error FROM summary_arbitration_outbox WHERE last_error IS NOT NULL",
    ):
        key = "connection_refused" if ("refused" in (err or "") or "WinError 10061" in (err or "")) else (err or "")[:80]
        arb_errors[key] += 1
    arb_acknowledged = sum(n for s, n, _, _ in arb if s == "ACKNOWLEDGED")

    # ---------- 发送端核验 ----------
    sender_tasks = []
    try:
        text = sender.stdout
        start = text.find("[")
        sender_tasks = json.loads(text[start:text.rfind("]") + 1]) if start >= 0 else []
    except Exception:
        pass
    sender_ok = (
        sender.returncode == 0
        and len(sender_tasks) == 2
        and all(t.get("confirmed_packet_count") == EXPECTED_PACKETS for t in sender_tasks)
        and all(t.get("failed_packet_count") == 0 and t.get("dropped_packet_count") == 0 for t in sender_tasks)
    )

    # ---------- Edge 侧核验 ----------
    edge_info = {}
    for container in ("edge_01", "edge_02"):
        decisions = read_edge_db(
            container,
            "SELECT COUNT(*) FROM bearing_decision_result WHERE revision=1",
        )[0][0]
        upload_states = dict(
            read_edge_db(container, "SELECT status, COUNT(*) FROM v12_result_upload GROUP BY status")
        )
        dead_letter = read_edge_db(
            container, "SELECT COUNT(*) FROM v12_result_upload WHERE status='DEAD_LETTER'"
        )[0][0]
        edge_info[container] = {
            "decisions_revision1": decisions,
            "v12_upload_states": upload_states,
            "v12_dead_letter": dead_letter,
        }

    # docker 日志队列错误
    log_errors = {}
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0 / 1e9))
    for container in ("edge_01", "edge_02"):
        proc = subprocess.run(
            ["docker", "logs", container, "--since", since],
            capture_output=True, timeout=90,
        )
        text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        log_errors[container] = {
            token: text.count(token)
            for token in ("QUEUE_FULL", "QUEUE_TIMEOUT", "DEAD_LETTER")
        }

    report = {
        "experiment_id": exp_id,
        "test": "ki08_kb23_extreme_gap3_conflict_rate",
        "sources": SOURCES,
        "t0_ns": t0,
        "t_end_ns": t_end,
        "duration_s": round((t_end - t0) / 1e9, 1),
        "conflict_rule": "cross_edge_action_level_gap_gte_3",
        "action_scorer_version": "action_scorer_v1",
        "conflict_semantics": "action_level_gap_v1",
        "level_conflict_threshold": LEVEL_CONFLICT_THRESHOLD,
        "conflict_rate": round(conflict_rate, 4) if conflict_rate is not None else None,
        "windows": {
            "total": total,
            "eligible": len(eligible),
            "conflicts": len(conflicts),
            "incomplete": len(incomplete),
            "excluded": excluded,
            "status_distribution": dict(q(sdb, "SELECT result_status, COUNT(*) FROM summary_window_result GROUP BY result_status")),
        },
        "level_gap_distribution": {str(k): v for k, v in sorted(level_gap_dist.items())},
        "level_by_edge": {e: dict(c) for e, c in level_by_edge.items()},
        "state_by_edge": {e: dict(c) for e, c in state_by_edge.items()},
        "new_action_level_conflict_count": len(conflicts),
        "legacy_binary_conflict_count": legacy_binary_conflicts,
        "windows_missing_gap_field": missing_gap,
        "conflict_details": conflict_details,
        "arbitration_not_uploaded": {
            "outbox_states": {s: n for s, n, _, _ in arb},
            "acknowledged": arb_acknowledged,
            "last_error_summary": dict(arb_errors),
        },
        "sender": {
            "ok": sender_ok,
            "returncode": sender.returncode,
            "tasks": [
                {
                    "task_id": t.get("task_id"),
                    "device_id": t.get("device_id"),
                    "bearing_id": t.get("bearing_id"),
                    "edge_node_id": t.get("target_edge_node_id"),
                    "confirmed": t.get("confirmed_packet_count"),
                    "failed": t.get("failed_packet_count"),
                    "dropped": t.get("dropped_packet_count"),
                }
                for t in sender_tasks
            ],
        },
        "edge": edge_info,
        "edge_log_error_counts": log_errors,
        "pass_conditions": {
            "sender_160_confirmed": sender_ok,
            "bearings_160": q(sdb, "SELECT COUNT(*) FROM summary_bearing_result")[0][0] == EXPECTED_BEARINGS,
            "windows_80": total == EXPECTED_WINDOWS,
            "incomplete_0": len(incomplete) == 0,
            "excluded_0": excluded == 0,
            "both_edges_participated": all(
                set(json.loads(w[4]).get("node_states", {}).keys()) == {"edge_01", "edge_02"}
                for w in eligible
            ) if eligible else False,
            "no_queue_errors": all(v == 0 for c in log_errors.values() for v in c.values()),
            "arbitration_never_delivered": arb_acknowledged == 0,
        },
    }
    dest = out_dir / "conflict_report.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    log(f"saved -> {dest}")
    log("=== 冲突率（新规则：跨 Edge action_level 跨度 >= 3） ===")
    log(f"  {len(conflicts)}/{len(eligible)} = {conflict_rate:.2%}" if conflict_rate is not None else "  N/A")
    log(f"  level_gap_distribution: { {str(k): v for k, v in sorted(level_gap_dist.items())} }")
    log(f"  level_by_edge: { {e: dict(c) for e, c in level_by_edge.items()} }")
    log(f"  state_by_edge: { {e: dict(c) for e, c in state_by_edge.items()} }")
    log(f"  legacy_binary_conflict_count (node_states 不一致): {legacy_binary_conflicts}")
    log("=== 仲裁不出本地 ===")
    log(f"  outbox: { {s: n for s, n, _, _ in arb} }  acknowledged={arb_acknowledged}")
    log("=== 通过条件 ===")
    for k, v in report["pass_conditions"].items():
        log(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
