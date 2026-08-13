"""Export the frozen A2 classifier while preserving its failed CV gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn

from .dataset import DEVELOPMENT_SPLIT, load_dataset_manifest
from .train import fit_final_model, read_feature_splits


MODEL_VERSION = "bearing-rf-a2-evaluation-v1"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_model(features_path: Path, output_dir: Path) -> dict:
    package_dir = Path(__file__).resolve().parent
    reports_dir = package_dir / "reports"
    schema_dir = package_dir / "schema"
    manifest_dir = package_dir / "manifests"
    report = _read_json(reports_dir / "cross_validation_report.json")
    if report.get("cv_gate_passed") is not False:
        raise RuntimeError("evaluation exporter requires the recorded failed CV gate")

    config = _read_json(package_dir / "experiments.json")
    manifest = load_dataset_manifest(manifest_dir)
    frame = read_feature_splits(features_path, {DEVELOPMENT_SPLIT})
    artifact = fit_final_model(frame, manifest, report["winner"], config)

    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "random_forest.joblib"
    joblib.dump(artifact, model_path, compress=3)
    loaded = joblib.load(model_path)
    if loaded["feature_columns"] != artifact["feature_columns"]:
        raise RuntimeError("model round-trip changed the feature contract")

    feature_schema = output_dir / "feature_schema.json"
    label_mapping = output_dir / "label_mapping.json"
    feature_schema.write_bytes((schema_dir / "feature_schema.json").read_bytes())
    label_mapping.write_bytes((schema_dir / "label_mapping.json").read_bytes())

    winner = report["winner"]
    model_manifest = {
        "schema_version": "bearing-rf-runtime-manifest/1.0",
        "model_version": MODEL_VERSION,
        "task": "binary_fault_detection",
        "deployment_status": "evaluation_only",
        "cv_gate_passed": False,
        "locked_test_consumed": False,
        "warning": (
            "This is a real fitted model, but bearing-isolated cross-validation "
            "did not meet the recorded deployment gate."
        ),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": artifact["experiment"],
        "training_bearing_ids": artifact["training_bearing_ids"],
        "training_rows": artifact["training_rows"],
        "feature_columns": artifact["feature_columns"],
        "labels": artifact["labels"],
        "random_seed": artifact["random_seed"],
        "random_forest_parameters": artifact["random_forest_parameters"],
        "cross_validation": {
            "window_macro_f1": winner["window_macro_f1"],
            "class_recall": winner["class_recall"],
            "bearing_majority_accuracy": winner["bearing_majority_accuracy"],
            "gates": report["gates"],
        },
        "model_sha256": _sha256(model_path),
        "feature_schema_sha256": _sha256(feature_schema),
        "label_mapping_sha256": _sha256(label_mapping),
        "training_features_sha256": _sha256(features_path),
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
    }
    manifest_path = output_dir / "model_manifest.json"
    _write_json(manifest_path, model_manifest)

    sums = {
        path.name: _sha256(path)
        for path in (model_path, manifest_path, feature_schema, label_mapping)
    }
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{digest.upper()}  {name}\n" for name, digest in sums.items()),
        encoding="ascii",
    )
    return model_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the frozen failed-gate model for integration evaluation"
    )
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = export_model(args.features.resolve(), args.output_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
