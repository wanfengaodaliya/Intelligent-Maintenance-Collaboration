from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import joblib
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from edge_diagnosis.random_forest_model import (
    FEATURE_COLUMNS,
    RandomForestDiagnosticModel,
)
from edge_model.contracts import PacketInferenceTask
from model_input_contract import model_input_probe


def _task(perception: dict | None = None) -> PacketInferenceTask:
    value = perception or model_input_probe()
    return PacketInferenceTask(
        request_id="request-1",
        device_id=value["device_id"],
        bearing_id=value["bearing_id"],
        task_id=value["task_id"],
        packet_id=value["packet_id"],
        sender_id=value["sender_id"],
        sequence_number=value["sequence_number"],
        perception=value,
    )


def _write_artifact(tmp_path: Path, constant: str = "healthy") -> tuple[Path, Path]:
    frame = pd.DataFrame(
        [[float(index)] * len(FEATURE_COLUMNS) for index in range(3)],
        columns=FEATURE_COLUMNS,
    )
    labels = ["healthy", "outer_ring_damage", "inner_ring_damage"]
    estimator = DummyClassifier(strategy="constant", constant=constant).fit(frame, labels)
    estimator.n_jobs = 6
    artifact = {
        "artifact_schema_version": "bearing-rf-integration-only/1.0",
        "feature_columns": list(FEATURE_COLUMNS),
        "labels": labels,
        "estimator": estimator,
    }
    model_path = tmp_path / "model.joblib"
    joblib.dump(artifact, model_path)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    metadata = {
        "schema_version": "bearing-rf-integration-metadata/1.0",
        "model_version": "bearing-rf-50ms-integration-only-v1",
        "model_sha256": digest,
        "feature_columns": list(FEATURE_COLUMNS),
        "labels": labels,
        "qualified_for_deployment": False,
        "allowed_use": "pipeline_integration_only",
        "locked_test_consumed": False,
    }
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return model_path, metadata_path


def test_runner_forces_single_thread_packet_inference(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)

    runner = RandomForestDiagnosticModel(model_path, metadata_path)

    assert runner.estimator.n_jobs == 1


@pytest.mark.parametrize(
    ("label", "edge_result", "risk"),
    [
        ("healthy", "normal", "low"),
        ("outer_ring_damage", "fault", "high"),
        ("inner_ring_damage", "fault", "high"),
    ],
)
def test_runner_maps_three_class_prediction_to_existing_edge_contract(
    tmp_path: Path, label: str, edge_result: str, risk: str
) -> None:
    model_path, metadata_path = _write_artifact(tmp_path, label)
    runner = RandomForestDiagnosticModel(model_path, metadata_path)

    result = runner.run(_task())

    assert result.edge_result == edge_result
    assert result.edge_risk_level == risk
    assert 0.0 <= result.confidence <= 1.0
    assert result.model_version == "bearing-rf-50ms-integration-only-v1"
    diagnosis = runner.last_diagnosis("request-1")
    assert diagnosis["diagnosis_label"] == label
    assert diagnosis["feature_columns"] == list(FEATURE_COLUMNS)
    assert diagnosis["deployment_status"] == "INTEGRATION_ONLY"


def test_warning_quality_is_inferred_but_marked_for_review(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)
    perception = model_input_probe()
    perception["perception_quality"] = {
        "status": "warning",
        "flags": ["VIBRATION_CONSTANT_SIGNAL"],
    }
    runner = RandomForestDiagnosticModel(model_path, metadata_path)

    runner.run(_task(perception))

    diagnosis = runner.last_diagnosis("request-1")
    assert diagnosis["review_required"] is True
    assert diagnosis["review_reasons"] == ["PERCEPTION_QUALITY_WARNING"]


def test_runner_rejects_missing_feature_without_default_value(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)
    perception = deepcopy(model_input_probe())
    del perception["features"]["vibration"]["rms"]
    runner = RandomForestDiagnosticModel(model_path, metadata_path)

    with pytest.raises(ValueError, match="missing=rms"):
        runner.run(_task(perception))


def test_runner_rejects_model_hash_mismatch(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["model_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        RandomForestDiagnosticModel(model_path, metadata_path)


def test_runner_rejects_feature_order_mismatch(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["feature_columns"] = list(reversed(FEATURE_COLUMNS))
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="feature_columns"):
        RandomForestDiagnosticModel(model_path, metadata_path)


def test_runner_rejects_artifact_claiming_deployment_qualification(tmp_path: Path) -> None:
    model_path, metadata_path = _write_artifact(tmp_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualified_for_deployment"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="integration-only"):
        RandomForestDiagnosticModel(model_path, metadata_path)
