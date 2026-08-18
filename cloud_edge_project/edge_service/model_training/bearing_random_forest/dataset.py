from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


DEVELOPMENT_SPLIT = "01_主开发集_真实损伤"
LOCKED_TEST_SPLIT = "02_最终测试集_锁定"
CHALLENGE_SPLIT = "03_混合损伤挑战集"
ARTIFICIAL_SPLIT = "04_人工损伤辅助集"
EXPECTED_SPLIT_COUNTS = {
    DEVELOPMENT_SPLIT: 14,
    LOCKED_TEST_SPLIT: 3,
    CHALLENGE_SPLIT: 3,
    ARTIFICIAL_SPLIT: 12,
}
VALID_LABELS = {"healthy", "outer_ring_damage", "inner_ring_damage"}


class DatasetLayoutError(ValueError):
    pass


@dataclass(frozen=True)
class BearingSpec:
    bearing_id: str
    label: str
    damage_source: str
    split: str
    source_directory: Path
    target_directory: Path
    mat_count: int
    validation_fold: int | None


@dataclass(frozen=True)
class DatasetManifest:
    root: Path
    bearings: tuple[BearingSpec, ...]

    @property
    def split_counts(self) -> dict[str, int]:
        return dict(Counter(item.split for item in self.bearings))

    @property
    def fold_counts(self) -> dict[int, int]:
        return dict(
            sorted(Counter(item.validation_fold for item in self.bearings if item.validation_fold).items())
        )


@dataclass(frozen=True)
class DatasetAudit:
    bearing_count: int
    mat_count: int
    errors: tuple[str, ...]


def _read_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise DatasetLayoutError(f"缺少清单文件: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise DatasetLayoutError(f"CSV 列不符合合同: {path.name}")
        return list(reader)


def load_dataset_manifest(root: Path | str) -> DatasetManifest:
    dataset_root = Path(root).resolve()
    split_rows = _read_csv(
        dataset_root / "dataset_split.csv",
        {
            "bearing_id",
            "label",
            "damage_source",
            "split",
            "source_directory",
            "target_directory",
            "mat_count",
        },
    )
    fold_rows = _read_csv(
        dataset_root / "cv_folds.csv",
        {"bearing_id", "label", "validation_fold"},
    )

    if len({row["bearing_id"] for row in split_rows}) != len(split_rows):
        raise DatasetLayoutError("dataset_split.csv 含重复轴承")
    by_id = {row["bearing_id"]: row for row in split_rows}
    folds: dict[str, int] = {}
    for row in fold_rows:
        bearing_id = row["bearing_id"]
        source = by_id.get(bearing_id)
        if source is None:
            raise DatasetLayoutError(f"交叉验证轴承不在数据清单: {bearing_id}")
        if source["split"] != DEVELOPMENT_SPLIT:
            raise DatasetLayoutError(f"cv_folds.csv 只能包含主开发集轴承: {bearing_id}")
        if source["label"] != row["label"]:
            raise DatasetLayoutError(f"交叉验证标签不一致: {bearing_id}")
        if bearing_id in folds:
            raise DatasetLayoutError(f"交叉验证轴承重复: {bearing_id}")
        fold = int(row["validation_fold"])
        if fold not in {1, 2, 3, 4}:
            raise DatasetLayoutError(f"交叉验证折号必须是 1..4: {bearing_id}")
        folds[bearing_id] = fold

    development_ids = {
        row["bearing_id"] for row in split_rows if row["split"] == DEVELOPMENT_SPLIT
    }
    if set(folds) != development_ids:
        raise DatasetLayoutError("四折清单必须恰好覆盖全部主开发集轴承")

    bearings: list[BearingSpec] = []
    for row in split_rows:
        if row["label"] not in VALID_LABELS:
            raise DatasetLayoutError(f"未知标签: {row['label']}")
        try:
            mat_count = int(row["mat_count"])
        except ValueError as exc:
            raise DatasetLayoutError(f"MAT 数量不是整数: {row['bearing_id']}") from exc
        bearings.append(
            BearingSpec(
                bearing_id=row["bearing_id"],
                label=row["label"],
                damage_source=row["damage_source"],
                split=row["split"],
                source_directory=Path(row["source_directory"]),
                target_directory=Path(row["target_directory"]),
                mat_count=mat_count,
                validation_fold=folds.get(row["bearing_id"]),
            )
        )
    return DatasetManifest(dataset_root, tuple(bearings))


def validate_dataset_layout(manifest: DatasetManifest) -> DatasetAudit:
    errors: list[str] = []
    total_mat = 0
    if manifest.split_counts != EXPECTED_SPLIT_COUNTS:
        errors.append(f"分组数量不匹配: {manifest.split_counts}")

    for item in manifest.bearings:
        directory = item.target_directory
        if not directory.is_dir():
            errors.append(f"轴承目录不存在: {directory}")
            continue
        files = sorted(directory.glob("*.mat"))
        total_mat += len(files)
        if item.mat_count != 80 or len(files) != 80:
            errors.append(
                f"{item.bearing_id} MAT 数量应为 80: manifest={item.mat_count}, actual={len(files)}"
            )
        pattern = re.compile(rf"(?:^|_){re.escape(item.bearing_id)}_(\d+)\.mat$", re.IGNORECASE)
        mismatched = [path.name for path in files if pattern.search(path.name) is None]
        if mismatched:
            errors.append(f"{item.bearing_id} 文件名不匹配: {mismatched[0]}")

    return DatasetAudit(len(manifest.bearings), total_mat, tuple(errors))
