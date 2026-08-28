# -*- coding: utf-8 -*-
"""测试前快照：服务状态 + DB 行数 + 空闲内存基线，全量只读。

用法：
    python snapshot.py --name pre_test --out-dir ../snapshots

输出：
    snapshots/pre_test_<ts>.json   （机器可读快照）
    控制台报告（人类可读）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import memory
from dpb_common import (
    CLOUD_EDGE_ROOT,
    CLOUD_URL,
    EDGE_CONTAINERS,
    EXPERIMENT_ID,
    NETWORK_URL,
    SCHEDULER_URL,
    SUMMARY_URL,
    http_get_json,
    read_cloud_db,
    read_edge_db,
    read_summary_db,
)

TOOLS_DIR = Path(__file__).parent
BENCH_DIR = TOOLS_DIR.parent


def edge_health_snapshot(url: str, container: str) -> dict:
    h = http_get_json(f"{url}/health")
    mq = h.get("mqtt_capacity") or {}
    mq_q = h.get("model_queue") or {}
    consumers = {f"consumer_{c['id']}_processed": c.get("processed") for c in mq_q.get("consumers", [])}
    db_count = None
    try:
        rows = read_edge_db(container, "SELECT COUNT(*) FROM bearing_decision_result")
        db_count = rows[0][0]
    except Exception as exc:  # noqa: BLE001
        db_count = f"ERROR: {exc}"
    return {
        "node_id": h.get("node_id"),
        "status": h.get("status"),
        "ready": h.get("ready"),
        "model_backend": h.get("model_backend"),
        "model_version": h.get("model_version"),
        "build_revision": h.get("build_revision"),
        "mqtt_connected": h.get("mqtt_connected"),
        "mqtt_queue_depth": mq.get("queue_depth"),
        "mqtt_capacity": mq.get("queue_capacity"),
        "mqtt_rejected_total": mq.get("rejected_total"),
        "mqtt_oversized_total": mq.get("oversized_total"),
        "model_queue_waiting": mq_q.get("waiting"),
        "model_queue_full_total": mq_q.get("queue_full_total"),
        "max_observed_queued": mq_q.get("max_observed_queued"),
        "model_consumer_count": mq_q.get("consumer_count"),
        "model_queue_capacity": mq_q.get("capacity"),
        "routing_pool": h.get("routing_pool"),
        "bearing_publisher": h.get("bearing_publisher"),
        **consumers,
        "inference_latency_ms": mq_q.get("inference_latency_ms"),
        "bearing_decision_result_rows": db_count,
    }


def summary_metrics_snapshot() -> dict:
    return http_get_json(f"{SUMMARY_URL}/summary/metrics")


def scheduler_snapshot() -> dict:
    health = http_get_json(f"{SCHEDULER_URL}/health")
    routing = None
    try:
        routing = http_get_json(f"{SCHEDULER_URL}/scheduler/routing-policy")
    except Exception as exc:  # noqa: BLE001
        routing = f"ERROR: {exc}"
    return {"health": health, "routing_policy": routing}


def network_snapshot() -> dict:
    out: dict = {}
    for name, url in [("health", f"{NETWORK_URL}/health"), ("links", f"{NETWORK_URL}/api/v1/network/links")]:
        try:
            out[name] = http_get_json(url)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"ERROR: {exc}"
    # 当前网络模式（experiment.yaml 第一段 mode）
    try:
        yaml_path = (
            CLOUD_EDGE_ROOT
            / "internet_service"
            / "network_simulator"
            / "config"
            / "experiment.yaml"
        )
        text = yaml_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("mode:"):
                out["mode"] = line.split(":", 1)[1].strip()
                break
    except Exception as exc:  # noqa: BLE001
        out["mode"] = f"ERROR: {exc}"
    return out


def db_rows_snapshot() -> dict:
    edge = {}
    for container in EDGE_CONTAINERS:
        try:
            rows = read_edge_db(
                container,
                "SELECT 'bearing_decision_result' AS t, COUNT(*) AS n FROM bearing_decision_result "
                "UNION ALL SELECT 'device_decision_result', COUNT(*) FROM device_decision_result "
                "UNION ALL SELECT 'device_decision_round', COUNT(*) FROM device_decision_round "
                "UNION ALL SELECT 'bearing_result_outbox', COUNT(*) FROM bearing_result_outbox "
                "UNION ALL SELECT 'bearing_result_delivery_history', COUNT(*) FROM bearing_result_delivery_history "
                "UNION ALL SELECT 'v12_result_upload', COUNT(*) FROM v12_result_upload",
            )
            edge[container] = {r[0]: r[1] for r in rows}
        except Exception as exc:  # noqa: BLE001
            edge[container] = f"ERROR: {exc}"
    try:
        rows = read_cloud_db(
            "SELECT 'cloud_moment_review_record' AS t, COUNT(*) AS n FROM cloud_moment_review_record "
            "UNION ALL SELECT 'device_arbitration_record', COUNT(*) FROM device_arbitration_record "
            "UNION ALL SELECT 'summary_window_record', COUNT(*) FROM summary_window_record "
            "UNION ALL SELECT 'edge_packet_summary', COUNT(*) FROM edge_packet_summary "
            "UNION ALL SELECT 'raw_packet_index', COUNT(*) FROM raw_packet_index "
            "UNION ALL SELECT 'packet_source_mapping', COUNT(*) FROM packet_source_mapping "
            "UNION ALL SELECT 'bearing_task_result', COUNT(*) FROM bearing_task_result "
            "UNION ALL SELECT 'device_task_result', COUNT(*) FROM device_task_result",
        )
        cloud = {r[0]: r[1] for r in rows}
    except Exception as exc:  # noqa: BLE001
        cloud = f"ERROR: {exc}"
    try:
        rows = read_summary_db(
            "SELECT 'summary_window_result' AS t, COUNT(*) AS n FROM summary_window_result "
            "UNION ALL SELECT 'summary_bearing_result', COUNT(*) FROM summary_bearing_result "
            "UNION ALL SELECT 'summary_suggestion_outbox', COUNT(*) FROM summary_suggestion_outbox "
            "UNION ALL SELECT 'summary_arbitration_outbox', COUNT(*) FROM summary_arbitration_outbox "
            "UNION ALL SELECT 'summary_window_publish_outbox', COUNT(*) FROM summary_window_publish_outbox "
            "UNION ALL SELECT 'summary_window_sync_outbox', COUNT(*) FROM summary_window_sync_outbox",
        )
        summary = {r[0]: r[1] for r in rows}
    except Exception as exc:  # noqa: BLE001
        summary = f"ERROR: {exc}"
    return {"edge": edge, "cloud": cloud, "summary": summary}


def cloud_service_snapshot() -> dict:
    out: dict = {}
    try:
        out["health"] = http_get_json(f"{CLOUD_URL}/health")
    except Exception as exc:  # noqa: BLE001
        out["health"] = f"ERROR: {exc}"
    return out


def take_snapshot(baseline_seconds: float, baseline_interval_ms: int) -> dict:
    snap = {
        "schema": "dual_phase_benchmark/snapshot/v1",
        "experiment_id": EXPERIMENT_ID,
        "captured_at_ns": time.time_ns(),
        "edge_01": edge_health_snapshot("http://127.0.0.1:8001", "edge_01"),
        "edge_02": edge_health_snapshot("http://127.0.0.1:8002", "edge_02"),
        "summary_metrics": summary_metrics_snapshot(),
        "scheduler": scheduler_snapshot(),
        "network_simulator": network_snapshot(),
        "cloud": cloud_service_snapshot(),
        "db_rows": db_rows_snapshot(),
    }
    # 空闲内存基线（30s）
    rows = memory.collect(baseline_seconds, baseline_interval_ms, None)
    snap["memory_idle_baseline"] = {
        "duration_s": baseline_seconds,
        "interval_ms": baseline_interval_ms,
        "sample_count": len(rows),
        "stats": memory.stats_of(rows),
    }
    return snap


def fmt_bytes(v: float | None) -> str:
    return f"{v / 1024 / 1024:.1f} MB" if v is not None else "-"


def print_report(snap: dict) -> None:
    e1, e2 = snap["edge_01"], snap["edge_02"]
    sm = snap["summary_metrics"]
    mem = snap["memory_idle_baseline"]["stats"]
    print("=" * 72)
    print(f"测试前快照 @ {snap['captured_at_ns']}")
    print("=" * 72)
    for label, e in (("edge_01", e1), ("edge_02", e2)):
        print(f"[{label}] {e['status']} ready={e['ready']} model={e['model_backend']} {e['model_version']}")
        print(f"    mqtt_q={e['mqtt_queue_depth']}/{e['mqtt_capacity']} rejected={e['mqtt_rejected_total']} oversized={e['mqtt_oversized_total']}")
        print(f"    model_q_waiting={e['model_queue_waiting']} queue_full={e['model_queue_full_total']} "
              f"processed={[e[k] for k in e if k.startswith('consumer_')]} "
              f"infer_lat={e['inference_latency_ms']}")
        print(f"    bearing_decision_result rows={e['bearing_decision_result_rows']}")
    print(f"[summary/metrics] total={sm['total_windows']} eligible={sm['eligible_windows']} "
          f"conflict={sm['conflict_windows']} incomplete={sm['incomplete_windows']} "
          f"conflict_rate={sm['conflict_rate']} "
          f"arb_upload={sm['arbitration_upload_windows']} arb_ack={sm['arbitration_acknowledged_windows']} "
          f"arb_success={sm['arbitration_upload_success_rate']}")
    sch = snap["scheduler"]["health"]
    print(f"[scheduler] {sch['status']} model={sch['model_backend']} edges online={sch['edge_nodes']['online']}/{sch['edge_nodes']['registered']}")
    print(f"[scheduler routing] {json.dumps(snap['scheduler']['routing_policy'], ensure_ascii=False)}")
    nw = snap["network_simulator"]
    print(f"[network] mode={nw.get('mode')} health={nw.get('health')} links={nw.get('links') if isinstance(nw.get('links'), str) else '...'}")
    if isinstance(nw.get("links"), dict) and isinstance(nw["links"].get("data"), list):
        links = nw["links"]["data"]
        print(f"    link count={len(links)}")
        for lk in links[:6]:
            print(f"    {json.dumps(lk, ensure_ascii=False)}")
    print(f"[cloud] {snap['cloud']['health']}")
    print("[db rows]")
    print(f"    edge_01 : {snap['db_rows']['edge'].get('edge_01')}")
    print(f"    edge_02 : {snap['db_rows']['edge'].get('edge_02')}")
    print(f"    cloud   : {snap['db_rows']['cloud']}")
    print(f"    summary : {snap['db_rows']['summary']}")
    print("[memory idle baseline]")
    for c in ("edge_01", "edge_02"):
        s = mem[c]
        print(f"    {c}: median={fmt_bytes(s['p50'])} "
              f"mean={fmt_bytes(s['mean'])} p95={fmt_bytes(s['p95'])} "
              f"max={fmt_bytes(s['max'])} peak_delta={fmt_bytes(s.get('peak_delta_from_median'))}")
    print("=" * 72)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="测试前快照采集")
    parser.add_argument("--name", type=str, default="pre_test", help="快照标签")
    parser.add_argument("--out-dir", type=str, default=str(BENCH_DIR / "snapshots"))
    parser.add_argument("--baseline-seconds", type=float, default=30.0, help="空闲内存基线采样时长")
    parser.add_argument("--baseline-interval-ms", type=int, default=500)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = take_snapshot(args.baseline_seconds, args.baseline_interval_ms)
    out_path = out_dir / f"{args.name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(snap)
    print(f"\nsaved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
