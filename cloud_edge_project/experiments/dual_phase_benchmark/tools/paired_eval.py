# -*- coding: utf-8 -*-
"""Edge/Cloud 配对评估器（指标①：能力保持率）。

数据流：
    1. Edge 结果：两个 Edge 容器 bearing_decision_result（is_current=1，测试窗口）
    2. Cloud 链路结果：cloud_moment_review_record（仲裁/重放产生的，按窗口序号索引）
    3. 缺失窗口重放：从 MAT 重建 50ms 窗口 → POST /cloud/infer（只补链路未覆盖的窗口）
    4. 配对键：task_id + window_sequence（Edge 侧通过重算 diagnosis_window_id 反解窗口序号）
    5. GT：ground_truth.json（数据集级标签，composite 数据集单独报告）

指标：
    - Macro-F1（GT 出现的类别取平均）、Accuracy、各类别 Recall
    - 能力保持率 = Edge Macro-F1 / Cloud Macro-F1 x 100%
    - 预测一致率 = Edge 与 Cloud 标签一致的窗口比例
    - Cloud 推理延迟（重放测得，供 TTFR 缩减率参考）

用法：
    python paired_eval.py --task-manifest tasks.json --mat-root D:\\Projects\\test_data --out report.json
    tasks.json: [{"task_id","sender_id","bearing_id","device_id","source_file"}]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from dpb_common import (
    CLOUD_EDGE_ROOT,
    CLOUD_URL,
    EDGE_CONTAINERS,
    read_cloud_db,
    read_edge_db,
    resolve_window_index,
    summarize,
)

TOOLS_DIR = Path(__file__).parent
BENCH_DIR = TOOLS_DIR.parent
GT_PATH = BENCH_DIR / "ground_truth.json"

CLOUD_EVALUATE_URL = f"{CLOUD_URL}/cloud/evaluate"
DIAGNOSIS_LABELS = {"healthy", "outer_ring_damage", "inner_ring_damage"}

# 在宿主机侧直接复用 sender 的 mat_reader（只读 MAT 文件）
PROJECT_ROOT = CLOUD_EDGE_ROOT
DEFAULT_SENDER_LOGS = PROJECT_ROOT / "sender_module" / "runtime" / "logs" / "packet_logs.jsonl"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "sender_module"))

from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id  # noqa: E402
from sender.mat_reader import load_mat_record  # noqa: E402

import numpy as np  # noqa: E402


def load_ground_truth() -> dict:
    return json.loads(GT_PATH.read_text(encoding="utf-8"))


def dataset_of_source(source_file: str) -> str:
    """从 MAT 文件名提取数据集代码（如 N09_M07_F10_K004_1.mat -> K004）。"""
    parts = source_file.replace("\\", "/").split("/")[-1].split("_")
    known_datasets = load_ground_truth()["datasets"]
    for part in parts:
        if part in known_datasets:
            return part
    raise ValueError(f"cannot parse dataset from source file: {source_file}")


def resolve_edge_window(edge_payload: dict, max_window: int = 80) -> int | None:
    """Edge 结果反解窗口序号（1-based）。"""
    return resolve_window_index(
        device_id=edge_payload["device_id"],
        task_id=edge_payload["task_id"],
        bearing_id=edge_payload["bearing_id"],
        sender_id=edge_payload["sender_id"],
        diagnosis_window_id=edge_payload.get("diagnosis_window_id", ""),
        max_window=max_window,
    )


def load_edge_predictions(
    since_ns: int, until_ns: int | None = None
) -> dict[tuple[str, int], dict]:
    """Edge revision-1 predictions indexed by task and window."""
    preds: dict[tuple[str, int], dict] = {}
    unresolved = 0
    for container in EDGE_CONTAINERS:
        rows = read_edge_db(
            container,
            "SELECT payload_json FROM bearing_decision_result WHERE revision=1",
        )
        for (payload_json,) in rows:
            p = json.loads(payload_json)
            if int(p.get("created_at_ns", 0)) < since_ns:
                continue
            if until_ns is not None and int(p.get("created_at_ns", 0)) > until_ns:
                continue
            diagnosis_label = p.get("diagnosis_label")
            if diagnosis_label not in DIAGNOSIS_LABELS:
                unresolved += 1
                continue
            seq = resolve_edge_window(p)
            if seq is None:
                unresolved += 1
                continue
            key = (p["task_id"], seq)
            cur = preds.get(key)
            if cur is None or p["created_at_ns"] < cur["created_at_ns"]:
                preds[key] = {
                    "task_id": p["task_id"],
                    "window": seq,
                    "edge_label": diagnosis_label,
                    "edge_state": p.get("bearing_state"),
                    "class_probabilities": p.get("class_probabilities"),
                    "confidence": p.get("confidence"),
                    "created_at_ns": p["created_at_ns"],
                    "edge_node": container,
                    "diagnosis_window_id": p["diagnosis_window_id"],
                }
    print(f"edge predictions: {len(preds)} windows (unresolved={unresolved})")
    return preds


def load_cloud_predictions(
    since_ns: int, until_ns: int | None = None
) -> dict[tuple[str, int], dict]:
    """Cloud 链路已有结果（cloud_moment_review_record）按 (task_id, window_seq) 索引。"""
    rows = read_cloud_db(
        "SELECT task_id, window_start_sequence, window_end_sequence, diagnosis_label, "
        "class_probabilities_json, confidence, model_version, created_at_ns "
        "FROM cloud_moment_review_record WHERE created_at_ns >= ? "
        "AND (? IS NULL OR created_at_ns <= ?)",
        (since_ns, until_ns, until_ns),
    )
    preds: dict[tuple[str, int], dict] = {}
    for task_id, wstart, wend, label, probabilities_json, conf, version, created_ns in rows:
        key = (task_id, wstart)
        preds[key] = {
            "task_id": task_id,
            "window": wstart,
            "cloud_label": label,
            "class_probabilities": (
                json.loads(probabilities_json) if probabilities_json else None
            ),
            "confidence": conf,
            "model_version": version,
            "created_at_ns": created_ns,
            "source": "chain" if wstart == wend else "chain-multi",
        }
    return preds


def build_cloud_request(
    task_id: str,
    sender_id: str,
    bearing_id: str,
    device_id: str,
    seq: int,
    window_data: dict,
    operating_context: dict,
    edge_result: str | None,
    edge_confidence: float | None,
) -> dict:
    """构造 cloud-infer/2.0 请求（单包 50ms 窗口）。"""
    packet_id = f"{task_id}_{bearing_id}_pkt_{seq:03d}"
    window_start_ns = seq * 50_000_000
    window_end_ns = window_start_ns + 50_000_000
    edge_perception = {
        "device_id": device_id,
        "task_id": task_id,
        "bearing_id": bearing_id,
        "sender_id": sender_id,
        "packet_id": packet_id,
        "sequence_number": seq,
        "edge_model_version": "distilled_h5_kd_fold3_a9f20442",
        "edge_inference": {
            "edge_result": edge_result or "normal",
            "confidence": edge_confidence if edge_confidence is not None else 0.5,
        },
        "features": {"operating_context": operating_context},
    }
    return {
        "schema_version": "cloud-infer/2.0",
        "decision_round_id": build_decision_round_id(
            device_id=device_id, task_id=task_id,
            window_start_sequence=seq, window_end_sequence=seq,
        ),
        "diagnosis_window_id": build_diagnosis_window_id(
            device_id=device_id, task_id=task_id, bearing_id=bearing_id,
            sender_id=sender_id, window_start_sequence=seq, window_end_sequence=seq,
        ),
        "edge_perception_result": edge_perception,
        "cloud_raw_window": {
            "device_id": device_id,
            "task_id": task_id,
            "bearing_id": bearing_id,
            "sender_id": sender_id,
            "window_start_sequence": seq,
            "window_end_sequence": seq,
            "window_start_ns": window_start_ns,
            "window_end_ns": window_end_ns,
            "contributing_packet_ids": [packet_id],
            "sample_rate_hz": 64000,
            "sample_count": 3200,
            "data": window_data,
        },
    }


def stats(values: np.ndarray) -> dict:
    return {
        "mean": float(np.mean(values)),
        "standard_deviation": float(np.std(values)),
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
    }


def replay_window(
    record,
    seq: int,
    edge_result: str | None,
    edge_confidence: float | None,
    max_tries: int = 2,
) -> dict:
    """Replay one window and return the raw three-class Cloud prediction."""
    windows = list(record.windows(duration_ms=50, count=seq))
    window = windows[seq - 1]
    speed = np.asarray(window.data["shaft_speed_rpm"]["values"], dtype=float)
    torque = np.asarray(window.data["load_torque_nm"]["values"], dtype=float)
    force = np.asarray(window.data["bearing_radial_load_n"]["values"], dtype=float)
    operating_context = {
        "shaft_speed_rpm": stats(speed),
        "load_torque_nm": stats(torque),
        "bearing_radial_load_n": stats(force),
        "bearing_module_temperature_c": float(window.data["bearing_module_temperature_c"]),
    }
    request = build_cloud_request(
        task_id=record.task_id,
        sender_id=record.sender_id,
        bearing_id=record.bearing_id,
        device_id=record.device_id,
        seq=seq,
        window_data=window.data,
        operating_context=operating_context,
        edge_result=edge_result,
        edge_confidence=edge_confidence,
    )
    last_error = None
    for _ in range(max_tries):
        t0 = time.perf_counter()
        try:
            resp = requests.post(CLOUD_EVALUATE_URL, json=request, timeout=180)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                result = resp.json()
                diagnosis_label = result.get("diagnosis_label")
                if diagnosis_label not in DIAGNOSIS_LABELS:
                    return {
                        "ok": False,
                        "error": "Cloud response is missing a valid diagnosis_label",
                    }
                return {
                    "ok": True,
                    "cloud_label": diagnosis_label,
                    "class_probabilities": result.get("class_probabilities"),
                    "confidence": result.get("confidence"),
                    "latency_ms": round(elapsed_ms, 1),
                    "model_version": result.get("model_version"),
                }
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    return {"ok": False, "error": last_error}


class TaskRecord:
    def __init__(self, task_id: str, sender_id: str, bearing_id: str, device_id: str,
                 source_file: str, mat_root: Path):
        self.task_id = task_id
        self.sender_id = sender_id
        self.bearing_id = bearing_id
        self.device_id = device_id
        self.source_file = source_file
        self.dataset = dataset_of_source(source_file)
        mat_path = Path(source_file)
        if not mat_path.is_absolute():
            mat_path = mat_root / mat_path
        self._record = load_mat_record(mat_path)

    def windows(self, *, duration_ms: int, count: int):
        return self._record.windows(duration_ms=duration_ms, count=count)

    def window_series(self) -> str:
        return f"{self.source_file}"


def run_eval(args) -> int:
    gt = load_ground_truth()
    manifest = json.loads(Path(args.task_manifest).read_text(encoding="utf-8"))
    mat_root = Path(args.mat_root)

    # 任务清单 -> TaskRecord（按 task_id 索引）
    tasks: dict[str, TaskRecord] = {}
    for item in manifest:
        rec = TaskRecord(
            task_id=item["task_id"],
            sender_id=item["sender_id"],
            bearing_id=item["bearing_id"],
            device_id=item["device_id"],
            source_file=item["source_file"],
            mat_root=mat_root,
        )
        tasks[rec.task_id] = rec

    edge_preds = load_edge_predictions(args.since_ns, args.until_ns)
    cloud_preds = load_cloud_predictions(args.since_ns, args.until_ns)
    generated_at: dict[tuple[str, int], int] = {}
    with Path(args.sender_logs).open("r", encoding="utf-8") as sender_file:
        for line in sender_file:
            record = json.loads(line)
            timestamp = int(record.get("end_generate_timestamp_ns", 0))
            if timestamp < args.since_ns:
                continue
            if args.until_ns is not None and timestamp > args.until_ns:
                continue
            generated_at[(record["task_id"], int(record["sequence_number"]))] = timestamp

    # 挂 Edge 结果到任务窗口（记录窗口序号）
    window_keys: list[tuple[str, int]] = []
    for (task_id, seq), pred in sorted(edge_preds.items()):
        if task_id in tasks:
            window_keys.append((task_id, seq))

    # 重放缺失窗口
    rows = []
    cloud_latencies = []
    for task_id, seq in window_keys:
        rec = tasks[task_id]
        gt_label = gt["datasets"][rec.dataset]["label"]
        composite = gt["datasets"][rec.dataset]["composite"]
        edge_prediction = edge_preds[(task_id, seq)]
        edge_state = edge_prediction["edge_state"]
        edge_label = edge_prediction["edge_label"]
        chain = cloud_preds.get((task_id, seq))
        t_gen = generated_at.get((task_id, seq))
        edge_ttfr_ms = (
            round((edge_prediction["created_at_ns"] - t_gen) / 1e6, 2)
            if t_gen is not None
            else None
        )
        if chain and chain["cloud_label"] and not args.replay_all:
            cloud_label = chain["cloud_label"]
            row = {
                "task_id": task_id, "window": seq, "dataset": rec.dataset,
                "source_file": rec.source_file, "composite": composite,
                "edge_label": edge_label, "edge_state_raw": edge_state,
                "cloud_label": cloud_label,
                "cloud_source": chain["source"],
                "cloud_latency_ms": None,
                "edge_ttfr_ms": edge_ttfr_ms,
                "gt_label": gt_label,
                "ok": True,
            }
        elif not args.chain_only:
            replay = replay_window(
                rec,
                seq,
                edge_result=edge_state,
                edge_confidence=edge_prediction["confidence"],
            )
            if replay.get("ok"):
                cloud_label = replay["cloud_label"]
                cloud_latencies.append(replay["latency_ms"])
                row = {
                    "task_id": task_id, "window": seq, "dataset": rec.dataset,
                    "source_file": rec.source_file, "composite": composite,
                    "edge_label": edge_label, "edge_state_raw": edge_state,
                    "cloud_label": cloud_label,
                    "cloud_source": "replay",
                    "cloud_latency_ms": replay["latency_ms"],
                    "edge_ttfr_ms": edge_ttfr_ms,
                    "gt_label": gt_label,
                    "ok": True,
                }
            else:
                row = {
                    "task_id": task_id, "window": seq, "dataset": rec.dataset,
                    "source_file": rec.source_file, "composite": composite,
                    "edge_label": edge_label, "edge_state_raw": edge_state,
                    "cloud_label": None, "cloud_source": None,
                    "cloud_latency_ms": None, "gt_label": gt_label,
                    "edge_ttfr_ms": edge_ttfr_ms,
                    "ok": False, "error": replay.get("error"),
                }
        else:
            continue
        rows.append(row)
        if len(rows) % 50 == 0:
            print(f"progress {len(rows)}/{len(window_keys)}")

    # ---- 指标计算（仅非 composite 数据集）----
    single = [r for r in rows if r["ok"] and not r["composite"]]
    composite = [r for r in rows if r["ok"] and r["composite"]]
    def per_model_stats(rows_subset: list[dict], pred_field: str) -> dict:
        if not rows_subset:
            return {"support": 0}
        correct = sum(1 for r in rows_subset if r[pred_field] == r["gt_label"])
        accuracy = correct / len(rows_subset)
        gt_classes = sorted({r["gt_label"] for r in rows_subset})
        recall = {}
        for cls in gt_classes:
            cls_rows = [r for r in rows_subset if r["gt_label"] == cls]
            recall[cls] = sum(1 for r in cls_rows if r[pred_field] == cls) / len(cls_rows)
        macro_f1 = {}
        f1s = []
        for cls in gt_classes:
            tp = sum(1 for r in rows_subset if r[pred_field] == cls and r["gt_label"] == cls)
            fp = sum(1 for r in rows_subset if r[pred_field] == cls and r["gt_label"] != cls)
            fn = sum(1 for r in rows_subset if r[pred_field] != cls and r["gt_label"] == cls)
            f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
            macro_f1[cls] = round(f1, 4)
            f1s.append(f1)
        return {
            "support": len(rows_subset),
            "accuracy": round(accuracy, 4),
            "recall": {c: round(v, 4) for c, v in recall.items()},
            "macro_f1": {c: round(v, 4) for c, v in macro_f1.items()},
            "macro_f1_avg": round(sum(f1s) / len(f1s), 4) if f1s else None,
            "label_distribution": dict(Counter(r[pred_field] for r in rows_subset)),
        }

    edge_stats = per_model_stats(single, "edge_label")
    cloud_stats = per_model_stats(single, "cloud_label")
    agreement = sum(
        1 for r in single if r["edge_label"] == r["cloud_label"]
    )
    retention = None
    if (
        edge_stats.get("macro_f1_avg") is not None
        and cloud_stats.get("macro_f1_avg") not in (None, 0)
    ):
        retention = round(edge_stats["macro_f1_avg"] / cloud_stats["macro_f1_avg"] * 100, 2)

    paired_ttfr_rows = [
        row for row in rows
        if row.get("cloud_source") == "replay"
        and row.get("edge_ttfr_ms") is not None
        and row.get("cloud_latency_ms") is not None
    ]
    paired_edge_ttfr = [row["edge_ttfr_ms"] for row in paired_ttfr_rows]
    paired_cloud_ttfr = [row["cloud_latency_ms"] for row in paired_ttfr_rows]
    paired_edge_summary = summarize(paired_edge_ttfr)
    paired_cloud_summary = summarize(paired_cloud_ttfr)
    edge_p50 = paired_edge_summary["p50"]
    cloud_p50 = paired_cloud_summary["p50"]

    report = {
        "schema": "dual_phase_benchmark/paired_eval/v1",
        "generated_at_ns": time.time_ns(),
        "since_ns": args.since_ns,
        "until_ns": args.until_ns,
        "window_count": len(rows),
        "single_fault_windows": len(single),
        "composite_windows": len(composite),
        "failed_windows": sum(1 for r in rows if not r["ok"]),
        "metrics_single_fault": {
            "edge": edge_stats,
            "cloud": cloud_stats,
            "capability_retention_macro_f1_pct": retention,
            "prediction_agreement_rate": round(agreement / len(single), 4) if single else None,
        },
        "cloud_replay_latency_ms": summarize(cloud_latencies),
        "paired_ttfr": {
            "note": "Classifier time-to-first-result on the same replayed windows; not token TTFT",
            "support": len(paired_ttfr_rows),
            "edge_ms": paired_edge_summary,
            "cloud_ms": paired_cloud_summary,
            "reduction_rate_p50": (
                round((1 - edge_p50 / cloud_p50) * 100, 2)
                if edge_p50 is not None and cloud_p50 not in (None, 0)
                else None
            ),
        },
        "composite_report": {
            "note": "KB23/KB24 复合损伤：排除三分类统计，单独报告",
            "edge_label_distribution": dict(Counter(r["edge_label"] for r in composite)),
            "cloud_label_distribution": dict(Counter(r["cloud_label"] for r in composite)),
            "agreement_rate": round(
                sum(1 for r in composite if r["edge_label"] == r["cloud_label"]) / len(composite), 4
            ) if composite else None,
        },
        "rows": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print(f"saved -> {out}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Edge/Cloud 配对评估器")
    parser.add_argument("--task-manifest", type=str, required=True,
                        help="[{\"task_id\",\"sender_id\",\"bearing_id\",\"device_id\",\"source_file\"}]")
    parser.add_argument(
        "--mat-root",
        type=str,
        default=os.getenv("DPB_MAT_ROOT", r"D:\Projects\test_data"),
    )
    parser.add_argument("--sender-logs", type=str, default=str(DEFAULT_SENDER_LOGS))
    parser.add_argument("--since-ns", type=int, required=True)
    parser.add_argument("--until-ns", type=int, default=None)
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument("--chain-only", action="store_true",
                        help="只统计链路已有 Cloud 结果，不重放缺失窗口")
    parser.add_argument(
        "--replay-all",
        action="store_true",
        help="对每个 Edge 窗口重放 Cloud；用于无选择偏差的正式配对评估",
    )
    args = parser.parse_args(argv)
    return run_eval(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
