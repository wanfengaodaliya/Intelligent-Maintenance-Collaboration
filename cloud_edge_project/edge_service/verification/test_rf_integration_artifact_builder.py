from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from edge_diagnosis.integration_artifact import (
    DEVELOPMENT_SPLIT,
    build_integration_artifact,
)
from edge_diagnosis.random_forest_model import FEATURE_COLUMNS


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _features(path: Path) -> None:
    rows = []
    labels = ["healthy", "outer_ring_damage", "inner_ring_damage"]
    for bearing_index, label in enumerate(labels):
        for row_index in range(4):
            row = {
                "source_bearing_code": f"DEV{bearing_index}",
                "split": DEVELOPMENT_SPLIT,
                "label": label,
                "perception_quality_status": "good",
            }
            row.update(
                {
                    name: float(bearing_index * 10 + row_index + feature_index / 100)
                    for feature_index, name in enumerate(FEATURE_COLUMNS)
                }
            )
            rows.append(row)
    locked = dict(rows[0])
    locked.update(source_bearing_code="K006", split="02_最终测试集_锁定")
    rows.append(locked)
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_builder_requires_explicit_integration_only_acknowledgement(tmp_path: Path) -> None:
    features_path = tmp_path / "features.parquet"
    _features(features_path)

    with pytest.raises(ValueError, match="acknowledge"):
        build_integration_artifact(
            features_path,
            tmp_path / "output",
            acknowledge_integration_only=False,
            expected_features_sha256=_sha256(features_path),
            n_estimators=10,
        )


def test_builder_uses_only_development_rows_and_writes_unqualified_metadata(
    tmp_path: Path,
) -> None:
    features_path = tmp_path / "features.parquet"
    output_dir = tmp_path / "output"
    _features(features_path)

    report = build_integration_artifact(
        features_path,
        output_dir,
        acknowledge_integration_only=True,
        expected_features_sha256=_sha256(features_path),
        n_estimators=10,
    )

    metadata = json.loads(
        (output_dir / "model_metadata.json").read_text(encoding="utf-8")
    )
    assert report["training_bearing_ids"] == ["DEV0", "DEV1", "DEV2"]
    assert "K006" not in report["training_bearing_ids"]
    assert metadata["qualified_for_deployment"] is False
    assert metadata["allowed_use"] == "pipeline_integration_only"
    assert metadata["locked_test_consumed"] is False
    assert metadata["feature_columns"] == list(FEATURE_COLUMNS)
    assert metadata["development_cv"]["status"] == "NOT_EVALUATED_FOR_THIS_ARTIFACT"
    assert metadata["source"]["sha256_verified"] is True
    assert (output_dir / "random_forest_integration_only.joblib").is_file()
    assert (output_dir / "SHA256SUMS").is_file()
    assert not (output_dir / "locked_test_report.json").exists()


def test_builder_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    features_path = tmp_path / "features.parquet"
    _features(features_path)

    with pytest.raises(ValueError, match="SHA-256"):
        build_integration_artifact(
            features_path,
            tmp_path / "output",
            acknowledge_integration_only=True,
            expected_features_sha256="0" * 64,
            n_estimators=10,
        )


def test_builder_hard_rejects_locked_bearing_even_if_split_is_mislabeled(
    tmp_path: Path,
) -> None:
    features_path = tmp_path / "features.parquet"
    _features(features_path)
    frame = pd.read_parquet(features_path)
    frame.loc[len(frame)] = {
        **frame.iloc[0].to_dict(),
        "source_bearing_code": "K006",
        "split": DEVELOPMENT_SPLIT,
    }
    frame.to_parquet(features_path, index=False)

    with pytest.raises(ValueError, match="locked test bearing"):
        build_integration_artifact(
            features_path,
            tmp_path / "output",
            acknowledge_integration_only=True,
            expected_features_sha256=_sha256(features_path),
            n_estimators=10,
        )
