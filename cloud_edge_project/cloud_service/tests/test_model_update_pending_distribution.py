"""Tests for the edge-facing pending-distribution discovery contract."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

import cloud_service.app as app_module
from cloud_service.config import load_cloud_settings
from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.service import ModelUpdateService
from cloud_service.storage.database import connect, initialize_database


class StaticTrainingDataSource:
    def load(self, update):
        return [
            {
                "sample_id": f"sample_{index}",
                "packet_id": f"packet_{index}",
                "task_id": f"task_{group}",
                "source_file": f"{group}.mat",
                "features": {"vibration": {"rms": float(index), "kurtosis": 3.0}},
                "historical_edge_result": {"label": "normal"},
                "cloud_label": "fault",
                "is_cloud_reviewed": True,
            }
            for index, group in enumerate(("a", "b", "c"), 1)
        ]


def _analysis(problem, *, analysis_id="analysis_001", underestimation=0.20):
    return {
        "analysis_id": analysis_id,
        "schema_version": "global_analysis_result/2.0",
        "scenario_type": "bearing",
        "subject_id": "machine_01",
        "problem_candidates": [problem],
        "packet_diagnosis_analysis": {
            "status": "succeeded",
            "reviewed_packet_count": 20,
            "cloud_correction_rate": underestimation,
            "risk_underestimation_rate": underestimation,
            "risk_overestimation_rate": 0.02,
        },
    }


def _problem(**changes):
    value = {
        "problem_id": "problem_001",
        "problem_layer": "packet_diagnosis",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "evidence": {"sample_count": 20},
        "persistence": "persistent",
        "suggested_action": "model_update",
    }
    value.update(changes)
    return value


def _save_analysis(database_path: Path, result, *, created_at_ns: int = 1):
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
                   analysis_id,scenario_type,subject_id,task_count,
                   reviewed_packet_count,cloud_correction_rate,result_json,created_at_ns
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                result["analysis_id"], result["scenario_type"], result["subject_id"],
                20, 20,
                result["packet_diagnosis_analysis"]["cloud_correction_rate"],
                json.dumps(result), created_at_ns,
            ),
        )


def _service(tmp_path: Path) -> ModelUpdateService:
    database_path = tmp_path / "cloud.db"
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(),
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
    )
    _save_analysis(database_path, _analysis(_problem()))
    return service


def _validation_results(manifest):
    return [
        {
            "sample_id": sample_id,
            "confirmed_label": manifest["sample_labels"][sample_id]["confirmed_label"],
            "label_source": manifest["sample_labels"][sample_id]["label_source"],
            "baseline_prediction": "normal",
            "candidate_prediction": manifest["sample_labels"][sample_id]["confirmed_label"],
            "problem_context": {"operating_condition": "high_load"},
        }
        for sample_id in manifest["test_sample_ids"]
    ]


def _advance_to_handoff(service: ModelUpdateService, tmp_path: Path) -> str:
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
            "feature_pipeline_version": "edge_feature_v1",
        }
    )["update"]
    prepared = service.prepare_data(created["update_id"])
    service.start_training(created["update_id"])
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"candidate")
    service.register_training_result(
        created["update_id"],
        {
            "candidate_version": "edge_v2",
            "artifact_path": "candidate.bin",
            "artifact_sha256": hashlib.sha256(b"candidate").hexdigest(),
            "model_type": "distilled_h5",
            "feature_pipeline_version": "edge_feature_v1",
            "input_feature_schema": {"vibration.rms": "number"},
            "training_dataset_id": prepared["training_dataset_id"],
            "training_config": {},
            "training_metrics": {},
        },
    )
    manifest = service.dataset_repository.get_by_update(created["update_id"])
    service.validate(created["update_id"], _validation_results(manifest))
    service.approve(created["update_id"], confirmed_by="operator")
    service.handoff_distribution(created["update_id"])
    return created["update_id"]


def test_pending_distribution_lists_edge_pull_for_handoff_task(tmp_path: Path) -> None:
    service = _service(tmp_path)
    update_id = _advance_to_handoff(service, tmp_path)

    pending = service.list_pending_distribution(edge_node_id="edge_01")

    assert pending["pending_pull_count"] == 1
    assert pending["pending_rollback_count"] == 0
    item = pending["pending_pulls"][0]
    assert item["update_id"] == update_id
    assert item["model_type"] == "distilled_h5"
    assert item["candidate_version"] == "edge_v2"
    assert item["baseline_version"] == "edge_v1"
    assert item["artifact_sha256"] == hashlib.sha256(b"candidate").hexdigest()
    assert item["target"]["family"] == "edge"


def test_pending_distribution_filters_by_explicit_edge_node_list(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    update_id = _advance_to_handoff(service, tmp_path)
    service.repository.update(
        update_id,
        distribution_result_json={
            **service.repository.get(update_id)["distribution_result"],
            "target": {
                "family": "edge",
                "deploy_to": "edge_node",
                "scope_subject_id": "machine_01",
                "edge_node_ids": ["edge_02"],
            },
        },
    )

    other = service.list_pending_distribution(edge_node_id="edge_01")
    targeted = service.list_pending_distribution(edge_node_id="edge_02")

    assert other["pending_pull_count"] == 0
    assert targeted["pending_pull_count"] == 1
    assert targeted["pending_pulls"][0]["update_id"] == update_id


def test_pending_distribution_excludes_cloud_family(tmp_path: Path) -> None:
    service = _service(tmp_path)
    update_id = _advance_to_handoff(service, tmp_path)
    service.repository.update(
        update_id,
        distribution_result_json={
            **service.repository.get(update_id)["distribution_result"],
            "target": {
                "family": "cloud",
                "deploy_to": "local_cloud",
                "cloud_node_id": "cloud_01",
            },
        },
    )

    pending = service.list_pending_distribution(edge_node_id="edge_01")

    assert pending["pending_pull_count"] == 0


def test_pending_distribution_lists_requested_rollback(tmp_path: Path) -> None:
    service = _service(tmp_path)
    update_id = _advance_to_handoff(service, tmp_path)
    service.repository.update(update_id, status="ineffective")
    service.request_rollback(update_id, requested_by="operator")

    pending = service.list_pending_distribution(edge_node_id="edge_01")

    assert pending["pending_pull_count"] == 0
    assert pending["pending_rollback_count"] == 1
    item = pending["pending_rollbacks"][0]
    assert item["update_id"] == update_id
    assert item["model_type"] == "distilled_h5"
    assert item["rollback_target_version"] == "edge_v1"


def test_edge_rollback_ack_clears_pending_rollback(tmp_path: Path) -> None:
    service = _service(tmp_path)
    update_id = _advance_to_handoff(service, tmp_path)
    service.repository.update(update_id, status="ineffective")
    service.request_rollback(update_id, requested_by="operator")

    acknowledged = service.record_rollback_result(
        update_id,
        {
            "status": "succeeded",
            "edge_node_id": "edge_01",
            "rollback_target_version": "edge_v1",
        },
    )

    assert acknowledged["status"] == "rolled_back"
    assert acknowledged["rollback_requested"] is False
    assert acknowledged["rollback_result"]["edge_ack"]["edge_node_id"] == "edge_01"
    assert service.list_pending_distribution(edge_node_id="edge_01")[
        "pending_rollback_count"
    ] == 0


def test_pending_distribution_api_endpoint(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(),
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
    )
    _save_analysis(database_path, _analysis(_problem()))
    update_id = _advance_to_handoff(service, tmp_path)
    settings = replace(load_cloud_settings(), backend="moment_light_adapt", database_path=database_path)
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: settings)
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as client:
        response = client.get(
            "/cloud/model-update/pending-distribution", params={"edge_node_id": "edge_01"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["edge_node_id"] == "edge_01"
    assert body["pending_pull_count"] == 1
    assert body["pending_pulls"][0]["update_id"] == update_id


def test_cloud_health_and_status_reporter_use_cloud_01_by_default() -> None:
    health_payload = app_module._health_payload(load_cloud_settings(), "ok")

    assert health_payload["node_id"] == "cloud_01"
    assert app_module.status_reporter.cloud_node_id == "cloud_01"
