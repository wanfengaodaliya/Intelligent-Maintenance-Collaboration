from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from cloud_edge_project.edge_service.src.edge_perception.config import PerceptionConfig
from cloud_edge_project.edge_service.src.edge_perception.contracts import (
    PerceptionInvocationContext,
)
from cloud_edge_project.edge_service.src.edge_perception.processor import EdgePerception
from .config import (
    FEATURE_EXTRACTOR_VERSION,
    training_perception_config,
)
from .dataset import (
    BearingSpec,
    DatasetManifest,
    load_dataset_manifest,
    validate_dataset_layout,
)
from .features import (
    flatten_perception_features,
)
from .packets import (
    build_offline_packet,
    parse_mat_filename,
)
from cloud_edge_project.sender_module.sender.mat_reader import load_mat_record


_OFFLINE_GENERATED_AT_NS = 9_000_000_000_000_000_000


class MatExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatExtraction:
    audit_records: tuple[dict[str, Any], ...]
    feature_rows: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ExtractionSummary:
    complete: bool
    bearing_count: int
    mat_count: int
    window_count: int
    rejected_window_count: int
    error_count: int


@dataclass(frozen=True)
class _BearingResult:
    bearing_id: str
    mat_count: int
    window_count: int
    jsonl_path: Path | None
    parquet_path: Path | None
    errors: tuple[dict[str, Any], ...]


def extract_mat(
    bearing: BearingSpec,
    mat_path: Path,
    *,
    bearing_ordinal: int,
    mat_ordinal: int,
    perception_config: PerceptionConfig,
    processor: EdgePerception | None = None,
) -> MatExtraction:
    metadata = parse_mat_filename(mat_path, bearing.bearing_id)
    try:
        record = load_mat_record(mat_path)
    except Exception as exc:
        return MatExtraction(
            (),
            (),
            (
                {
                    "source_bearing_code": bearing.bearing_id,
                    "source_mat_file": mat_path.name,
                    "window_index": "",
                    "stage": "load_mat",
                    "error_code": "MAT_READ_FAILED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "failed_window_count": 80,
                },
            ),
        )
    perception = processor or EdgePerception(
        perception_config,
        clock_ns=lambda: _OFFLINE_GENERATED_AT_NS,
    )
    audit_records: list[dict[str, Any]] = []
    feature_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for window in record.windows(duration_ms=50, count=80):
        try:
            packet = build_offline_packet(
                window,
                bearing_ordinal=bearing_ordinal,
                mat_ordinal=mat_ordinal,
            )
        except Exception as exc:
            errors.append(
                {
                    "source_bearing_code": bearing.bearing_id,
                    "source_mat_file": mat_path.name,
                    "window_index": window.sequence_number,
                    "stage": "packet_validation",
                    "error_code": "PACKET_REJECTED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "failed_window_count": 1,
                }
            )
            continue
        context = PerceptionInvocationContext(
            edge_node_id="edge_01",
            perception_received_at_ns=packet["end_generate_timestamp_ns"] + 1,
        )
        downsampled = perception.downsample(packet, context)
        if not downsampled.status.success or downsampled.payload is None:
            sample_counts = {
                name: value["sample_count"]
                for name, value in window.data.items()
                if isinstance(value, dict)
            }
            errors.append(
                {
                    "source_bearing_code": bearing.bearing_id,
                    "source_mat_file": mat_path.name,
                    "window_index": window.sequence_number,
                    "stage": "downsampling",
                    "error_code": downsampled.status.error_code,
                    "error_type": "EdgeRejection",
                    "message": json.dumps(sample_counts, ensure_ascii=False, sort_keys=True),
                    "failed_window_count": 1,
                }
            )
            continue
        result = perception.perceive(downsampled.payload, context)
        if not result.status.success or result.payload is None:
            errors.append(
                {
                    "source_bearing_code": bearing.bearing_id,
                    "source_mat_file": mat_path.name,
                    "window_index": window.sequence_number,
                    "stage": "perception",
                    "error_code": result.status.error_code,
                    "error_type": "EdgeRejection",
                    "message": "边缘感知模块拒绝该窗口",
                    "failed_window_count": 1,
                }
            )
            continue

        source = {
            "source_bearing_code": bearing.bearing_id,
            "source_mat_file": mat_path.name,
            "operating_condition": metadata.operating_condition,
            "repeat_index": metadata.repeat_index,
            "window_index": window.sequence_number,
            "label": bearing.label,
            "damage_source": bearing.damage_source,
            "split": bearing.split,
            "validation_fold": bearing.validation_fold,
        }
        quality = result.payload["perception_quality"]
        feature_rows.append(
            {
                **source,
                "perception_quality_status": quality["status"],
                "perception_quality_flags": json.dumps(
                    quality["flags"], ensure_ascii=False
                ),
                **flatten_perception_features(result.payload),
            }
        )
        audit_records.append(
            {"offline_source": source, "perception_result": result.payload}
        )

    rejected_windows = sum(error["failed_window_count"] for error in errors)
    if len(audit_records) + rejected_windows != 80:
        raise MatExtractionError(
            f"窗口审计数量必须为 80: {bearing.bearing_id}/{mat_path.name}"
        )
    return MatExtraction(tuple(audit_records), tuple(feature_rows), tuple(errors))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _extract_bearing(
    bearing: BearingSpec,
    *,
    bearing_ordinal: int,
    all_bearings: tuple[BearingSpec, ...],
    output_dir: Path,
    mat_limit: int | None,
    perception_config: PerceptionConfig,
) -> _BearingResult:
    shard_dir = output_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = shard_dir / f"{bearing.bearing_id}.jsonl"
    parquet_path = shard_dir / f"{bearing.bearing_id}.parquet"
    marker_path = shard_dir / f"{bearing.bearing_id}.complete.json"
    available = sorted(
        bearing.target_directory.glob("*.mat"),
        key=lambda path: (
            parse_mat_filename(path, bearing.bearing_id).operating_condition,
            parse_mat_filename(path, bearing.bearing_id).repeat_index,
        ),
    )
    selected = available[:mat_limit] if mat_limit is not None else available
    expected_windows = len(selected) * 80

    if marker_path.is_file() and jsonl_path.is_file() and parquet_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            marker.get("mat_count") == len(selected)
            and marker.get("window_count", 0) + marker.get("rejected_window_count", 0)
            == expected_windows
            and marker.get("jsonl_sha256") == _sha256(jsonl_path)
            and marker.get("parquet_sha256") == _sha256(parquet_path)
        ):
            return _BearingResult(
                bearing.bearing_id,
                len(selected),
                int(marker["window_count"]),
                jsonl_path,
                parquet_path,
                tuple(marker.get("errors", [])),
            )

    processor = EdgePerception(
        perception_config,
        clock_ns=lambda: _OFFLINE_GENERATED_AT_NS,
    )
    temporary_jsonl = jsonl_path.with_suffix(".jsonl.tmp")
    feature_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    audited_mats = 0
    with temporary_jsonl.open("w", encoding="utf-8", newline="\n") as jsonl:
        for local_mat_ordinal, mat_path in enumerate(selected, start=1):
            mat_ordinal = 1 + sum(
                item.mat_count for item in all_bearings[:bearing_ordinal - 1]
            ) + local_mat_ordinal - 1
            try:
                extraction = extract_mat(
                    bearing,
                    mat_path,
                    bearing_ordinal=bearing_ordinal,
                    mat_ordinal=mat_ordinal,
                    perception_config=perception_config,
                    processor=processor,
                )
            except Exception as exc:
                errors.append(
                    {
                        "source_bearing_code": bearing.bearing_id,
                        "source_mat_file": mat_path.name,
                        "window_index": "",
                        "stage": "extract_mat",
                        "error_code": "UNEXPECTED_EXTRACTION_FAILURE",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "failed_window_count": 80,
                    }
                )
                audited_mats += 1
                continue
            for audit_record in extraction.audit_records:
                jsonl.write(
                    json.dumps(audit_record, ensure_ascii=False, allow_nan=False) + "\n"
                )
            feature_rows.extend(extraction.feature_rows)
            errors.extend(extraction.errors)
            audited_mats += 1

    rejected_windows = sum(int(error["failed_window_count"]) for error in errors)
    if audited_mats != len(selected) or len(feature_rows) + rejected_windows != expected_windows:
        raise MatExtractionError(f"轴承窗口未完整审计: {bearing.bearing_id}")

    temporary_parquet = parquet_path.with_suffix(".parquet.tmp")
    pd.DataFrame.from_records(feature_rows).to_parquet(
        temporary_parquet,
        engine="pyarrow",
        index=False,
        compression="zstd",
    )
    temporary_jsonl.replace(jsonl_path)
    temporary_parquet.replace(parquet_path)
    _write_json(
        marker_path,
        {
            "bearing_id": bearing.bearing_id,
            "mat_count": audited_mats,
            "window_count": len(feature_rows),
            "rejected_window_count": rejected_windows,
            "errors": errors,
            "jsonl_sha256": _sha256(jsonl_path),
            "parquet_sha256": _sha256(parquet_path),
        },
    )
    return _BearingResult(
        bearing.bearing_id,
        audited_mats,
        len(feature_rows),
        jsonl_path,
        parquet_path,
        tuple(errors),
    )


def _combine_shards(results: list[_BearingResult], output_dir: Path) -> None:
    jsonl_output = output_dir / "perception_records.jsonl"
    jsonl_temporary = jsonl_output.with_suffix(".jsonl.tmp")
    with jsonl_temporary.open("wb") as destination:
        for result in results:
            assert result.jsonl_path is not None
            with result.jsonl_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    destination.write(chunk)
    jsonl_temporary.replace(jsonl_output)

    frames = []
    for result in results:
        assert result.parquet_path is not None
        frames.append(pq.read_table(result.parquet_path).to_pandas())
    combined = pd.concat(frames, ignore_index=True)
    combined["validation_fold"] = pd.array(
        combined["validation_fold"], dtype="Int64"
    )
    combined["perception_quality_flags"] = combined[
        "perception_quality_flags"
    ].map(
        lambda value: value
        if isinstance(value, str)
        else json.dumps(list(value), ensure_ascii=False)
    )
    parquet_output = output_dir / "features.parquet"
    parquet_temporary = parquet_output.with_suffix(".parquet.tmp")
    combined.to_parquet(
        parquet_temporary,
        engine="pyarrow",
        index=False,
        compression="zstd",
    )
    parquet_temporary.replace(parquet_output)


def _dependency_versions() -> dict[str, str]:
    names = (
        "numpy",
        "scipy",
        "pandas",
        "pyarrow",
        "scikit-learn",
        "joblib",
    )
    return {name: package_metadata.version(name) for name in names}


def extract_dataset(
    manifest: DatasetManifest,
    output_dir: Path | str,
    *,
    bearing_ids: set[str] | None = None,
    mat_limit_per_bearing: int | None = None,
    workers: int = 1,
) -> ExtractionSummary:
    if not 1 <= workers <= 6:
        raise ValueError("workers 必须位于 1..6")
    if mat_limit_per_bearing is not None and mat_limit_per_bearing < 1:
        raise ValueError("mat_limit_per_bearing 必须为正整数")
    selected = tuple(
        item
        for item in manifest.bearings
        if bearing_ids is None or item.bearing_id in bearing_ids
    )
    if not selected:
        raise ValueError("没有选中任何轴承")
    if bearing_ids is not None and {item.bearing_id for item in selected} != bearing_ids:
        missing = sorted(bearing_ids - {item.bearing_id for item in selected})
        raise ValueError(f"未知轴承: {', '.join(missing)}")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    perception_config = training_perception_config()
    ordinals = {
        item.bearing_id: index for index, item in enumerate(manifest.bearings, start=1)
    }
    results_by_id: dict[str, _BearingResult] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _extract_bearing,
                item,
                bearing_ordinal=ordinals[item.bearing_id],
                all_bearings=manifest.bearings,
                output_dir=destination,
                mat_limit=mat_limit_per_bearing,
                perception_config=perception_config,
            ): item.bearing_id
            for item in selected
        }
        for future in as_completed(futures):
            result = future.result()
            results_by_id[result.bearing_id] = result

    results = [results_by_id[item.bearing_id] for item in selected]
    errors = [error for result in results for error in result.errors]
    error_path = destination / "extraction_errors.csv"
    with error_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = (
            "source_bearing_code",
            "source_mat_file",
            "window_index",
            "stage",
            "error_code",
            "error_type",
            "message",
            "failed_window_count",
        )
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(errors)

    expected_windows = sum(result.mat_count for result in results) * 80
    rejected_windows = sum(
        int(error["failed_window_count"]) for error in errors
    )
    complete = (
        all(result.jsonl_path is not None for result in results)
        and sum(result.window_count for result in results) + rejected_windows
        == expected_windows
    )
    if complete:
        _combine_shards(results, destination)

    summary = ExtractionSummary(
        complete=complete,
        bearing_count=len(selected),
        mat_count=sum(result.mat_count for result in results),
        window_count=sum(result.window_count for result in results),
        rejected_window_count=rejected_windows,
        error_count=len(errors),
    )
    config = perception_config
    extraction_manifest = {
        "schema_version": "bearing-rf-extraction/1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "feature_extractor_version": FEATURE_EXTRACTOR_VERSION,
        "window_duration_ms": 50,
        "windows_per_mat": 80,
        "workers": workers,
        "selected_bearings": [item.bearing_id for item in selected],
        "mat_limit_per_bearing": mat_limit_per_bearing,
        "counts": {
            "bearings": summary.bearing_count,
            "mats": summary.mat_count,
            "windows": summary.window_count,
            "expected_windows": expected_windows,
            "rejected_windows": summary.rejected_window_count,
            "errors": summary.error_count,
        },
        "dataset_manifest_sha256": _sha256(manifest.root / "dataset_split.csv"),
        "cv_folds_sha256": _sha256(manifest.root / "cv_folds.csv"),
        "fir": {
            "path": str(config.fir_coefficients_path),
            "sha256": config.fir_sha256,
            "asset_version": config.fir_asset_version,
        },
        "thresholds": {
            "source": config.numerical_threshold_source,
            "version": config.numerical_threshold_version,
            "running_speed_threshold_rpm": config.running_speed_threshold_rpm,
        },
        "runtime": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "dependencies": _dependency_versions(),
        },
    }
    if complete:
        extraction_manifest["artifacts"] = {
            name: _sha256(destination / name)
            for name in ("perception_records.jsonl", "features.parquet", "extraction_errors.csv")
        }
    _write_json(destination / "extraction_manifest.json", extraction_manifest)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 Paderborn 轴承随机森林特征")
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--bearing-id", action="append")
    parser.add_argument("--mat-limit-per-bearing", type=int)
    args = parser.parse_args()

    manifest = load_dataset_manifest(args.dataset_root)
    if args.bearing_id is None and args.mat_limit_per_bearing is None:
        layout = validate_dataset_layout(manifest)
        if layout.errors:
            raise SystemExit("数据布局审计失败: " + "; ".join(layout.errors))
    summary = extract_dataset(
        manifest,
        args.output_dir,
        bearing_ids=set(args.bearing_id) if args.bearing_id else None,
        mat_limit_per_bearing=args.mat_limit_per_bearing,
        workers=args.workers,
    )
    print(json.dumps(asdict(summary), ensure_ascii=False))
    return 0 if summary.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
