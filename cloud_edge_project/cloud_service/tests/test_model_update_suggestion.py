from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

import cloud_service.app as app_module
from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.model_update import suggestion as suggestion_module
from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.service import ModelUpdateService
from cloud_service.storage.database import connect, initialize_database


def _settings(tmp_path: Path) -> CloudSettings:
    return CloudSettings(
        backend="moment_light_adapt",
        vllm_url="http://127.0.0.1:1/v1/chat/completions",
        vllm_model_name="test-model",
        vllm_api_key="",
        vllm_timeout_seconds=1.0,
        database_path=tmp_path / "cloud.db",
    )


def _task(**changes) -> dict:
    value = {
        "update_id": "update_sug_001",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "evidence_snapshot": {"sample_count": 20, "cloud_correction_rate": 0.20},
        "model_type": "distilled_h5",
        "baseline_version": "edge_v1",
        "trainer_plan": {
            "trainer_id": "distilled_h5_kd_trainer",
            "output_dir": "/tmp/out",
        },
    }
    value.update(changes)
    return value


def test_build_suggestion_prompt_contains_structured_fields() -> None:
    prompt = suggestion_module.build_suggestion_prompt(_task())
    assert "risk_underestimation" in prompt
    assert "high_load" in prompt
    assert "sample_count" in prompt
    assert "distilled_h5" in prompt
    assert "edge_v1" in prompt


def test_template_suggestion_contains_problem_and_sample_count() -> None:
    text = suggestion_module.template_suggestion(_task())
    assert "risk_underestimation" in text
    assert "20" in text
    assert "edge_v1" in text


def test_generate_suggestion_returns_llm_text_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_generate_text(messages, settings, **kwargs):
        return "建议基于 edge_v1 微调蒸馏 H5 模型，收集 20 条高负载样本。"

    monkeypatch.setattr(suggestion_module, "generate_text", fake_generate_text)
    text, source = suggestion_module.generate_suggestion(_task(), _settings(tmp_path))
    assert source == "llm"
    assert "微调" in text


def test_generate_suggestion_falls_back_to_template_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    def failing_generate_text(messages, settings, **kwargs):
        raise CloudServiceError("CLOUD_UNAVAILABLE", "unavailable", 503)

    monkeypatch.setattr(suggestion_module, "generate_text", failing_generate_text)
    text, source = suggestion_module.generate_suggestion(_task(), _settings(tmp_path))
    assert source == "template"
    assert "risk_underestimation" in text


def _save_analysis(database_path: Path) -> None:
    problem = {
        "problem_id": "problem_001",
        "problem_layer": "packet_diagnosis",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "evidence": {
            "sample_count": 20,
            "cloud_correction_rate": 0.20,
            "risk_underestimation_rate": 0.20,
            "risk_overestimation_rate": 0.02,
        },
        "persistence": "persistent",
        "suggested_action": "model_update",
    }
    result = {
        "analysis_id": "analysis_001",
        "schema_version": "global_analysis_result/2.0",
        "scenario_type": "bearing",
        "subject_id": "machine_01",
        "problem_candidates": [problem],
        "packet_diagnosis_analysis": {
            "status": "succeeded",
            "reviewed_packet_count": 20,
            "cloud_correction_rate": 0.20,
        },
    }
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
                   analysis_id,scenario_type,subject_id,task_count,
                   reviewed_packet_count,cloud_correction_rate,result_json,created_at_ns
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                result["analysis_id"], result["scenario_type"], result["subject_id"],
                20, 20, 0.20, json.dumps(result), time.time_ns(),
            ),
        )


def _service(tmp_path: Path) -> ModelUpdateService:
    database_path = tmp_path / "cloud.db"
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=None,
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
        settings=_settings(tmp_path),
    )
    _save_analysis(database_path)
    return service


def test_create_auto_generates_template_suggestion(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    suggestion = created["suggestion"]
    assert suggestion is not None
    assert suggestion["source"] == "template"
    assert "risk_underestimation" in suggestion["text"]
    assert suggestion["generated_at_ns"] > 0


def test_generate_suggestion_persists_and_regenerates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    refreshed = service.generate_suggestion(created["update_id"])
    suggestion = refreshed["suggestion"]
    assert suggestion["source"] == "template"
    assert suggestion["text"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    settings = replace(
        load_cloud_settings(), backend="moment_light_adapt", database_path=database_path
    )
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: settings)

    class _LoadedRunner:
        loaded = True
        model_version = "moment-scl05-final"
        gpu_available = False

    monkeypatch.setattr(app_module, "preload_moment_runner", lambda settings: None)
    monkeypatch.setattr(app_module, "get_moment_runner", lambda settings: _LoadedRunner())
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        yield test_client


def _insert_task(database_path: Path, *, update_id: str) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO model_update_task(
                   update_id,analysis_id,problem_id,scenario_type,subject_id,
                   problem_type,problem_context_json,evidence_snapshot_json,
                   baseline_version,status,created_at_ns,updated_at_ns
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                update_id, "analysis_001", "problem_001", "bearing", "machine_01",
                "risk_underestimation",
                json.dumps({"operating_condition": "high_load"}),
                json.dumps({"sample_count": 20}),
                "edge_v1", "created", time.time_ns(), time.time_ns(),
            ),
        )


def test_suggestion_endpoint_generates_and_returns(client, tmp_path) -> None:
    database_path = tmp_path / "cloud.db"
    _insert_task(database_path, update_id="update_sug_api")

    response = client.post("/cloud/model-update/update_sug_api/suggestion")

    assert response.status_code == 200
    body = response.json()
    assert body["update_id"] == "update_sug_api"
    assert body["suggestion"]["source"] == "template"
    assert "risk_underestimation" in body["suggestion"]["text"]

    fetched = client.get("/cloud/model-update/update_sug_api")
    assert fetched.status_code == 200
    assert fetched.json()["suggestion"]["text"] == body["suggestion"]["text"]


def test_suggestion_endpoint_missing_task_returns_404(client) -> None:
    response = client.post("/cloud/model-update/update_sug_missing/suggestion")
    assert response.status_code == 404
    assert response.json()["error_code"] == "UPDATE_NOT_FOUND"
