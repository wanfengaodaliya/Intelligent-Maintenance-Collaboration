from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .features import FEATURE_COLUMNS


@dataclass(frozen=True)
class AuditReport:
    passed: bool
    bearing_count: int
    mat_count: int
    window_count: int
    rejected_window_count: int
    jsonl_line_count: int
    error_record_count: int
    errors: tuple[str, ...]


def audit_run(
    run_dir: Path | str,
    *,
    expected_bearings: int = 32,
    expected_mats: int = 2_560,
    expected_windows: int = 204_800,
) -> AuditReport:
    root = Path(run_dir).resolve()
    errors: list[str] = []
    required = (
        "perception_records.jsonl",
        "features.parquet",
        "extraction_errors.csv",
        "extraction_manifest.json",
    )
    for name in required:
        if not (root / name).is_file():
            errors.append(f"缺少产物: {name}")
    if errors:
        return AuditReport(False, 0, 0, 0, 0, 0, 0, tuple(errors))

    features = pd.read_parquet(root / "features.parquet")
    missing_features = [name for name in FEATURE_COLUMNS if name not in features.columns]
    if missing_features:
        errors.append(f"缺少特征列: {missing_features}")
    elif not np.isfinite(features.loc[:, FEATURE_COLUMNS].to_numpy(dtype=float)).all():
        errors.append("特征表含 NaN 或无穷值")

    window_count = len(features)
    successful_per_mat = features.groupby(
        ["source_bearing_code", "source_mat_file"]
    ).size().to_dict()
    if not set(features["perception_quality_status"]) <= {"good", "warning"}:
        errors.append("存在非法感知质量状态")

    with (root / "perception_records.jsonl").open("r", encoding="utf-8") as handle:
        jsonl_line_count = sum(1 for _ in handle)
    with (root / "extraction_errors.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        error_rows = list(csv.DictReader(handle))
    rejected_per_mat: dict[tuple[str, str], int] = {}
    for row in error_rows:
        key = (row["source_bearing_code"], row["source_mat_file"])
        rejected_per_mat[key] = rejected_per_mat.get(key, 0) + int(
            row["failed_window_count"]
        )
    rejected_window_count = sum(rejected_per_mat.values())
    all_mat_keys = set(successful_per_mat) | set(rejected_per_mat)
    bearing_count = len({key[0] for key in all_mat_keys})
    mat_count = len(all_mat_keys)
    for key in all_mat_keys:
        accounted = successful_per_mat.get(key, 0) + rejected_per_mat.get(key, 0)
        if accounted != 80:
            errors.append(f"MAT 窗口未完整记账: {key}, accounted={accounted}")
    manifest = json.loads((root / "extraction_manifest.json").read_text(encoding="utf-8"))

    checks = (
        (bearing_count, expected_bearings, "轴承数"),
        (mat_count, expected_mats, "MAT 数"),
        (jsonl_line_count, window_count, "JSONL 与 Parquet 成功窗口数"),
        (window_count + rejected_window_count, expected_windows, "成功与拒绝窗口总数"),
    )
    for actual, expected, name in checks:
        if actual != expected:
            errors.append(f"{name}不匹配: expected={expected}, actual={actual}")
    if manifest.get("complete") is not True:
        errors.append("提取清单未标记 complete=true")
    counts = manifest.get("counts", {})
    if counts.get("windows") != window_count:
        errors.append("提取清单成功窗口数不匹配")
    if counts.get("rejected_windows") != rejected_window_count:
        errors.append("提取清单拒绝窗口数不匹配")
    if counts.get("expected_windows") != expected_windows:
        errors.append("提取清单预期窗口数不匹配")

    return AuditReport(
        not errors,
        bearing_count,
        mat_count,
        window_count,
        rejected_window_count,
        jsonl_line_count,
        len(error_rows),
        tuple(errors),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="审计随机森林特征提取产物")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    report = audit_run(args.run_dir)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.passed or not args.require_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
