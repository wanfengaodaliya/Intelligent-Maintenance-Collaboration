from __future__ import annotations

import shutil
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from edge_diagnosis import (
    DEFAULT_MODEL_DIR,
    RUNTIME_MODEL_VERSION,
    ModelArtifactError,
    RandomForestDiagnosticModel,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig
from edge_model.contracts import EdgeResult, EXECUTION_LOCAL_MODEL, PacketInferenceTask
from edge_model.model_client import ModelClient
from edge_model.pipeline import EdgeModelPipeline
from model_input_contract import model_input_probe
from edge_service import app as edge_service_app


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


def test_model_accepts_metadata_checked_out_with_crlf_line_endings(tmp_path) -> None:
    copied = tmp_path / "model"
    shutil.copytree(DEFAULT_MODEL_DIR, copied)
    for name in ("feature_schema.json", "label_mapping.json", "model_manifest.json"):
        path = copied / name
        normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        path.write_bytes(normalized.replace("\n", "\r\n").encode("utf-8"))

    model = RandomForestDiagnosticModel(copied)

    assert model.model_version == RUNTIME_MODEL_VERSION


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


def test_rf_infer_endpoint_runs_the_real_model_with_complete_perception() -> None:
    response = TestClient(edge_service_app.app).post(
        "/edge/rf/infer", json=model_input_probe()
    )

    assert response.status_code == 200
    result = response.json()
    assert result["task_id"] == "task-model-probe"
    assert result["model_name"] == RUNTIME_MODEL_VERSION
    assert result["label"] in {"normal", "abnormal"}
    assert result["need_cloud"] is True
    assert result["feature_extractor_version"] == "edge-perception-v1"
    assert result["feature_schema_version"] == "bearing-rf-features/1.0"
    assert result["model_input_schema_version"] == "edge-model-input/1.1"


def test_rf_public_adapter_maps_fault_to_abnormal() -> None:
    edge = EdgeResult(
        edge_result="fault",
        confidence=0.731,
        edge_risk_level="high",
        model_version=RUNTIME_MODEL_VERSION,
    )

    result = edge_service_app.public_rf_result(_task(), edge, edge_latency_ms=12.5)

    assert result["label"] == "abnormal"
    assert result["confidence"] == 0.731
    assert result["risk_level"] == "high"
    assert result["need_cloud"] is True
    assert result["model_name"] == RUNTIME_MODEL_VERSION


def test_edge_health_exposes_active_rule_backend() -> None:
    response = TestClient(edge_service_app.app).get("/health")

    assert response.status_code == 200
    result = response.json()
    assert result["model_backend"] == "rule"
    assert result["model_version"] == "edge_rule_test_v1"
    assert result["model_deployment_status"] == "built_in_rule"
    assert result["feature_extractor_version"] == "edge-perception-v1"
    assert result["feature_schema_version"] is None
    assert result["model_input_schema_version"] is None


def test_v01_edge_infer_keeps_its_four_sensor_contract() -> None:
    response = TestClient(edge_service_app.app).post(
        "/edge/infer",
        json={
            "task_id": "task_0001",
            "scenario": "industrial",
            "source_node": "edge_1",
            "task_type": "fault_detection",
            "timestamp": "2026-06-20 10:00:00",
            "deadline_ms": 200,
            "priority": 0.8,
            "data_size_kb": 128,
            "data": {
                "device_id": "machine_01",
                "temperature": 78.5,
                "vibration": 0.63,
                "current": 13.2,
                "load": 0.76,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["model_name"] == "edge_small_model"
