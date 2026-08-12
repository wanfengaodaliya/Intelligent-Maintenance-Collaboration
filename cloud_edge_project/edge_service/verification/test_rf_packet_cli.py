from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import pandas as pd
from sklearn.dummy import DummyClassifier

from edge_diagnosis.random_forest_model import FEATURE_COLUMNS
from model_input_contract import model_input_probe


def _artifact(tmp_path: Path) -> tuple[Path, Path]:
    labels = ["healthy", "outer_ring_damage", "inner_ring_damage"]
    frame = pd.DataFrame([[0.0] * len(FEATURE_COLUMNS)] * 3, columns=FEATURE_COLUMNS)
    estimator = DummyClassifier(strategy="constant", constant="healthy").fit(frame, labels)
    model_path = tmp_path / "model.joblib"
    joblib.dump(
        {
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


def test_cli_function_preserves_identity_and_marks_temporary_use(tmp_path: Path) -> None:
    from edge_diagnosis.cli import diagnose_packet

    model_path, metadata_path = _artifact(tmp_path)
    result = diagnose_packet(model_input_probe(), model_path, metadata_path)

    assert result["device_id"] == "device-model-probe"
    assert result["packet_id"] == "packet-model-probe"
    assert result["sequence_number"] == 1
    assert result["edge_result"] == "normal"
    assert result["diagnosis_label"] == "healthy"
    assert result["diagnosis_mode"] == "TEMP_PACKET_MODEL"
    assert result["window_ms"] == 50
    assert result["deployment_status"] == "INTEGRATION_ONLY"
