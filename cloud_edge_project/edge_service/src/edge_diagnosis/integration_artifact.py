"""Build an explicitly unqualified 50 ms artifact for edge integration tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier

from .random_forest_model import FEATURE_COLUMNS, LABELS


DEVELOPMENT_SPLIT = "01_主开发集_真实损伤"
MODEL_VERSION = "bearing-rf-50ms-integration-only-v1"
LOCKED_TEST_BEARING_IDS = frozenset({"K006", "KA30", "KI17"})
CANONICAL_FEATURES_SHA256 = "33bc3c5496a62446f78a06422d3e315c76183a0b6b5f320b23c6119311a1990d"
FIRST_STAGE_CV = {
    "experiment": "A2",
    "macro_f1": 0.569355275255826,
    "class_recall": {
        "healthy": 0.6073825503355704,
        "outer_ring_damage": 0.5401328903654485,
        "inner_ring_damage": 0.5186907020872865,
    },
    "gate_passed": False,
    "gate_macro_f1_minimum": 0.90,
    "gate_each_class_recall_minimum": 0.85,
}


def build_integration_artifact(
    features_path: Path | str,
    output_dir: Path | str,
    *,
    acknowledge_integration_only: bool,
    expected_features_sha256: str,
    n_estimators: int = 300,
    random_seed: int = 20260811,
) -> dict:
    if acknowledge_integration_only is not True:
        raise ValueError(
            "must acknowledge integration-only use with "
            "--acknowledge-integration-only"
        )
    if n_estimators < 1:
        raise ValueError("n_estimators must be positive")
    source = Path(features_path).resolve()
    destination = Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError("features parquet does not exist: %s" % source)
    source_sha256 = _sha256(source)
    if source_sha256 != expected_features_sha256:
        raise ValueError("features parquet SHA-256 does not match expected value")
    destination.mkdir(parents=True, exist_ok=True)
    columns = [
        "source_bearing_code",
        "split",
        "label",
        "perception_quality_status",
        *FEATURE_COLUMNS,
    ]
    frame = pd.read_parquet(
        source,
        columns=columns,
        filters=[("split", "==", DEVELOPMENT_SPLIT)],
        engine="pyarrow",
    )
    if frame.empty or set(frame["split"]) != {DEVELOPMENT_SPLIT}:
        raise ValueError("development split filtering failed")
    leaked_ids = sorted(set(frame["source_bearing_code"]) & LOCKED_TEST_BEARING_IDS)
    if leaked_ids:
        raise ValueError(
            "locked test bearing present in development rows: %s"
            % ",".join(leaked_ids)
        )
    frame = frame[frame["perception_quality_status"] == "good"].copy()
    if set(frame["label"]) != set(LABELS):
        raise ValueError("development data must contain exactly three labels")
    numeric = frame.loc[:, FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=np.float64)).all():
        raise ValueError("development features must be finite")
    frame.loc[:, FEATURE_COLUMNS] = numeric
    counts = frame.groupby("source_bearing_code").size()
    rows_per_bearing = int(counts.min())
    balanced = pd.concat(
        [
            group.sample(n=rows_per_bearing, random_state=random_seed)
            for _, group in frame.groupby("source_bearing_code", sort=True)
        ]
    ).sort_index()
    sample_weight = np.full(len(balanced), 1.0 / rows_per_bearing, dtype=np.float64)
    estimator = RandomForestClassifier(
        n_estimators=n_estimators,
        criterion="gini",
        max_depth=None,
        min_samples_split=2,
        min_samples_leaf=2,
        max_features="sqrt",
        bootstrap=True,
        class_weight="balanced",
        n_jobs=6,
        random_state=random_seed,
    )
    estimator.fit(
        balanced.loc[:, FEATURE_COLUMNS],
        balanced["label"],
        sample_weight=sample_weight,
    )
    artifact = {
        "artifact_schema_version": "bearing-rf-integration-only/1.0",
        "model_version": MODEL_VERSION,
        "feature_columns": list(FEATURE_COLUMNS),
        "labels": list(LABELS),
        "random_seed": random_seed,
        "training_bearing_ids": sorted(set(balanced["source_bearing_code"])),
        "training_rows": len(balanced),
        "estimator": estimator,
    }
    model_path = destination / "random_forest_integration_only.joblib"
    model_temporary = model_path.with_suffix(".joblib.tmp")
    joblib.dump(artifact, model_temporary)
    model_temporary.replace(model_path)
    metadata = {
        "schema_version": "bearing-rf-integration-metadata/1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "model_sha256": _sha256(model_path),
        "feature_columns": list(FEATURE_COLUMNS),
        "labels": list(LABELS),
        "qualified_for_deployment": False,
        "allowed_use": "pipeline_integration_only",
        "locked_test_consumed": False,
        "challenge_test_consumed": False,
        "development_cv": (
            FIRST_STAGE_CV
            if source_sha256 == CANONICAL_FEATURES_SHA256
            else {
                "status": "NOT_EVALUATED_FOR_THIS_ARTIFACT",
                "gate_passed": False,
            }
        ),
        "training": {
            "split": DEVELOPMENT_SPLIT,
            "bearing_ids": artifact["training_bearing_ids"],
            "bearing_count": len(artifact["training_bearing_ids"]),
            "rows_per_bearing": rows_per_bearing,
            "training_rows": len(balanced),
            "feature_count": len(FEATURE_COLUMNS),
            "n_estimators": n_estimators,
            "random_seed": random_seed,
        },
        "source": {
            "features_path": str(source),
            "features_sha256": source_sha256,
            "sha256_verified": True,
        },
        "runtime": {
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
    }
    metadata_path = destination / "model_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    sums = destination / "SHA256SUMS"
    sums.write_text(
        "%s  %s\n%s  %s\n"
        % (
            _sha256(model_path),
            model_path.name,
            _sha256(metadata_path),
            metadata_path.name,
        ),
        encoding="utf-8",
    )
    return {
        "model_path": str(model_path),
        "metadata_path": str(metadata_path),
        "model_sha256": metadata["model_sha256"],
        "training_bearing_ids": artifact["training_bearing_ids"],
        "training_rows": len(balanced),
        "qualified_for_deployment": False,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
