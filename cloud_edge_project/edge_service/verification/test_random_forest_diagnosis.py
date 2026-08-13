from __future__ import annotations

import shutil
from copy import deepcopy

import pytest

from edge_diagnosis import (
    DEFAULT_MODEL_DIR,
    RUNTIME_MODEL_VERSION,
    ModelArtifactError,
    RandomForestDiagnosticModel,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.contracts import EXECUTION_LOCAL_MODEL, PacketInferenceTask
from edge_model.model_client import ModelClient
from edge_model.pipeline import EdgeModelPipeline
from model_input_contract import model_input_probe


def _task() -> PacketInferenceTask:
    perception = model_input_probe()
    return PacketInferenceTask(
        request_id="rf-probe",
        device_id=perception["device_id"],
        bearing_id=perception["bearing_id"],
        task_id=perception["task_id"],
        packet_id=perception["packet_id"],
        sender_id=perception["sender_id"],
        sequence_number=perception["sequence_number"],
        perception=perception,
    )


def test_committed_random_forest_loads_and_matches_edge_result_contract() -> None:
    model = RandomForestDiagnosticModel()

    result = model.run(_task())

    assert model.deployment_status == "evaluation_only"
    assert len(model.feature_columns) == 27
    assert result.edge_result in {"normal", "fault"}
    assert result.edge_risk_level in {"low", "high"}
    assert 0.0 <= result.confidence <= 1.0
    assert result.model_version == RUNTIME_MODEL_VERSION


def test_model_rejects_corrupted_committed_artifact(tmp_path) -> None:
    copied = tmp_path / "model"
    shutil.copytree(DEFAULT_MODEL_DIR, copied)
    with (copied / "random_forest.joblib").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ModelArtifactError, match="checksum mismatch"):
        RandomForestDiagnosticModel(copied)


def test_model_rejects_non_numeric_feature() -> None:
    model = RandomForestDiagnosticModel()
    task = _task()
    task.perception["features"]["vibration"]["rms"] = True

    with pytest.raises(ValueError):
        model.run(task)


def test_local_pipeline_records_real_model_execution() -> None:
    records = []
    results = []
    completions = []
    config = EdgeModelConfig()
    assert config.diagnostic_backend == "local"
    pipeline = EdgeModelPipeline(
        config,
        ModelClient(ModelClientConfig()),
        RandomForestDiagnosticModel(),
        on_run_record=records.append,
        on_packet_result=results.append,
        on_packet_completed=completions.append,
    )
    perception = deepcopy(model_input_probe())

    pipeline.start()
    try:
        pipeline.ingest(perception["sender_id"], perception)
    finally:
        pipeline.stop()

    assert len(records) == len(results) == len(completions) == 1
    assert records[0].execution_mode == EXECUTION_LOCAL_MODEL
    assert records[0].model_version == RUNTIME_MODEL_VERSION
    assert results[0].edge.model_version == RUNTIME_MODEL_VERSION
    assert completions[0].status == "SUCCEEDED"
