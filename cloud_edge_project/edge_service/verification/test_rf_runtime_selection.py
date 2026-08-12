from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from edge_diagnosis import MockDiagnosticModel, RandomForestDiagnosticModel
from edge_diagnosis.factory import diagnostic_runner_from_environment
from edge_diagnosis.random_forest_model import FEATURE_COLUMNS
from edge_model.config import EdgeModelConfig


def _artifact(tmp_path: Path) -> tuple[Path, Path]:
    labels = ["healthy", "outer_ring_damage", "inner_ring_damage"]
    frame = pd.DataFrame(
        [[0.0] * len(FEATURE_COLUMNS)] * 3,
        columns=FEATURE_COLUMNS,
    )
    estimator = DummyClassifier(strategy="constant", constant="healthy").fit(
        frame, labels
    )
    model_path = tmp_path / "model.joblib"
    joblib.dump(
        {
            "artifact_schema_version": "bearing-rf-integration-only/1.0",
            "feature_columns": list(FEATURE_COLUMNS),
            "labels": labels,
            "estimator": estimator,
        },
        model_path,
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "bearing-rf-integration-metadata/1.0",
                "model_version": "bearing-rf-50ms-integration-only-v1",
                "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                "feature_columns": list(FEATURE_COLUMNS),
                "labels": labels,
                "qualified_for_deployment": False,
                "allowed_use": "pipeline_integration_only",
                "locked_test_consumed": False,
            }
        ),
        encoding="utf-8",
    )
    return model_path, metadata_path


def test_mock_remains_the_default_backend() -> None:
    config = EdgeModelConfig()

    runner = diagnostic_runner_from_environment(config, {})

    assert isinstance(runner, MockDiagnosticModel)
    assert config.diagnostic_backend == "mock"


def test_http_backend_keeps_mock_as_its_code_fallback() -> None:
    config = EdgeModelConfig(diagnostic_backend="http")

    runner = diagnostic_runner_from_environment(config, {})

    assert isinstance(runner, MockDiagnosticModel)


def test_rf_backend_requires_both_artifact_paths() -> None:
    config = EdgeModelConfig(diagnostic_backend="rf_50ms_integration")

    with pytest.raises(ValueError, match="EDGE_RF_MODEL_PATH.*EDGE_RF_METADATA_PATH"):
        diagnostic_runner_from_environment(config, {})


def test_rf_backend_builds_random_forest_runner(tmp_path: Path) -> None:
    model_path, metadata_path = _artifact(tmp_path)
    config = EdgeModelConfig(diagnostic_backend="rf_50ms_integration")

    runner = diagnostic_runner_from_environment(
        config,
        {
            "EDGE_RF_MODEL_PATH": str(model_path),
            "EDGE_RF_METADATA_PATH": str(metadata_path),
        },
    )

    assert isinstance(runner, RandomForestDiagnosticModel)


def test_edge_model_config_rejects_unknown_backend() -> None:
    config = EdgeModelConfig(diagnostic_backend="mystery")

    assert any("diagnostic_backend" in error for error in config.validate())
