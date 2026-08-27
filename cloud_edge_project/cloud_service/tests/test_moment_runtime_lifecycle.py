from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cloud_service.service as service_module
from cloud_service.app import app
from cloud_service.config import CloudSettings
from cloud_service.model_update.model_types import ActiveModelVersionStore
from cloud_service.model_update.repository import ModelUpdateRepository
from cloud_service.moment_review_repository import MomentReviewRepository
from cloud_service.storage.database import initialize_database
from cloud_service.storage.schema import DDL


def test_existing_v22_database_adds_raw_diagnosis_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud-v22.db"
    old_ddl = DDL.replace("    diagnosis_label TEXT,\n", "").replace(
        "    class_probabilities_json TEXT,\n", ""
    )
    connection = sqlite3.connect(database_path)
    connection.executescript(old_ddl)
    connection.close()

    initialize_database(database_path)

    connection = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(cloud_moment_review_record)"
        ).fetchall()
    }
    connection.close()
    assert {"diagnosis_label", "class_probabilities_json"} <= columns


def _moment_result() -> dict[str, object]:
    return {
        "review_id": "moment_window_001",
        "result_id": "cloud_window_001",
        "schema_version": "cloud-bearing-result/2.0",
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "sender_id": "edge_01",
        "decision_round_id": "round_01",
        "diagnosis_window_id": "window_001",
        "window_start_sequence": 1,
        "window_end_sequence": 1,
        "window_start_ns": 0,
        "window_end_ns": 50_000_000,
        "bearing_state": "normal",
        "edge_label": "normal",
        "confidence": 0.95,
        "data_quality_score": 1.0,
        "risk_level": "low",
        "action_grade": 0,
        "recommended_action": "continue_operation",
        "model_version": "moment-scl05-final",
        "created_at_ns": 1,
    }


def test_moment_repository_can_save_into_a_fresh_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"

    repository = MomentReviewRepository(database_path)
    repository.save(_moment_result())

    assert repository.get("moment_window_001")["model_version"] == "moment-scl05-final"


def test_cloud_lifespan_initializes_schema_before_accepting_requests(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "fresh.db"
    monkeypatch.setenv("CLOUD_BACKEND", "moment_light_adapt")
    monkeypatch.setenv("CLOUD_REVIEW_DB_PATH", str(database_path))
    monkeypatch.setenv("GLOBAL_ANALYSIS_POLL_SECONDS", "0")

    class _LoadedRunner:
        loaded = True
        model_version = "moment-scl05-final"
        gpu_available = False

    monkeypatch.setattr("cloud_service.app.preload_moment_runner", lambda settings: None)
    monkeypatch.setattr("cloud_service.app.get_moment_runner", lambda settings: _LoadedRunner())

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with sqlite3.connect(database_path) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='cloud_moment_review_record'"
            ).fetchone()

    assert table == ("cloud_moment_review_record",)


class _FakeRunner:
    fail_load = False

    def __init__(
        self, settings: CloudSettings, *, model_version: str = "moment-scl05-final"
    ) -> None:
        self.settings = settings
        self.model_version = model_version
        self.loaded = False

    def load(self) -> None:
        if self.fail_load:
            raise RuntimeError("candidate cannot be loaded")
        self.loaded = True


def _settings(tmp_path: Path) -> CloudSettings:
    return CloudSettings(
        backend="moment_light_adapt",
        vllm_url="",
        vllm_model_name="",
        vllm_api_key="",
        vllm_timeout_seconds=1,
        database_path=tmp_path / "cloud.db",
        moment_checkpoint_path=tmp_path / "old.pt",
        moment_condition_norm_path=tmp_path / "old_norm.json",
        moment_pretrained_path=tmp_path / "pretrained",
        moment_deployment_dir=tmp_path / "deployment",
    )


def _candidate_artifact(tmp_path: Path) -> dict[str, str]:
    artifact_dir = tmp_path / "candidate"
    artifact_dir.mkdir()
    checkpoint = artifact_dir / "best_model.pt"
    checkpoint.write_bytes(b"candidate checkpoint")
    (artifact_dir / "condition_norm.json").write_text(
        '{"mean":[0.0],"std":[1.0]}', encoding="utf-8"
    )
    return {
        "artifact_path": str(artifact_dir),
        "artifact_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
    }


def test_candidate_runner_is_loaded_before_it_replaces_the_active_runner(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    old_runner = _FakeRunner(settings)
    old_runner.load()
    monkeypatch.setattr(service_module, "_moment_runner", old_runner)
    monkeypatch.setattr(service_module, "_moment_runner_settings", settings)

    new_runner = service_module.activate_moment_candidate(
        settings,
        _candidate_artifact(tmp_path),
        "moment_candidate_v2",
        runner_factory=_FakeRunner,
    )

    assert new_runner.loaded is True
    assert new_runner.model_version == "moment_candidate_v2"
    assert service_module.get_moment_runner(settings) is new_runner


def test_failed_candidate_load_keeps_the_previous_runner(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    old_runner = _FakeRunner(settings)
    old_runner.load()
    monkeypatch.setattr(service_module, "_moment_runner", old_runner)
    monkeypatch.setattr(service_module, "_moment_runner_settings", settings)
    monkeypatch.setattr(_FakeRunner, "fail_load", True)

    with pytest.raises(RuntimeError, match="candidate cannot be loaded"):
        service_module.activate_moment_candidate(
            settings,
            _candidate_artifact(tmp_path),
            "moment_candidate_v2",
            runner_factory=_FakeRunner,
        )

    assert service_module.get_moment_runner(settings) is old_runner


def test_activate_moment_version_reloads_the_default_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    candidate_runner = _FakeRunner(settings, model_version="moment_candidate_v2")
    candidate_runner.load()
    monkeypatch.setattr(service_module, "_moment_runner", candidate_runner)
    monkeypatch.setattr(service_module, "_moment_runner_settings", settings)

    baseline_runner = service_module.activate_moment_version(
        settings,
        "moment-scl05-final",
        runner_factory=_FakeRunner,
    )

    assert baseline_runner.loaded is True
    assert baseline_runner.model_version == "moment-scl05-final"
    assert baseline_runner.settings.moment_checkpoint_path == settings.moment_checkpoint_path
    assert service_module.get_moment_runner(settings) is baseline_runner


def test_candidate_activation_rejects_tampered_condition_normalization(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    artifact = _candidate_artifact(tmp_path)
    artifact_dir = Path(artifact["artifact_path"])
    norm_path = artifact_dir / "condition_norm.json"
    artifact["artifact_bundle"] = {
        "entries": [
            {
                "rel_path": "best_model.pt",
                "sha256": artifact["artifact_sha256"],
            },
            {
                "rel_path": "condition_norm.json",
                "sha256": hashlib.sha256(norm_path.read_bytes()).hexdigest(),
            },
        ],
        "artifact_sha256": artifact["artifact_sha256"],
    }
    norm_path.write_text('{"mean":[9.0],"std":[1.0]}', encoding="utf-8")

    with pytest.raises(ValueError, match="MOMENT_CANDIDATE_CHECKSUM_MISMATCH"):
        service_module.activate_moment_candidate(
            settings,
            artifact,
            "moment_candidate_v2",
            runner_factory=_FakeRunner,
        )


def test_preload_restores_the_persisted_active_candidate_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    artifact = _candidate_artifact(tmp_path)
    ModelUpdateRepository(settings.database_path).create(
        {
            "update_id": "update_cloud_001",
            "analysis_id": "analysis_cloud_001",
            "problem_id": "problem_cloud_001",
            "scenario_type": "bearing",
            "subject_id": "device_01",
            "problem_type": "condition_weakness",
            "model_type": "moment_light_adapt",
            "problem_context_json": {},
            "evidence_snapshot_json": {},
            "baseline_version": "moment-scl05-final",
            "candidate_version": "moment_candidate_v2",
            "candidate_artifact_json": {
                **artifact,
                "candidate_version": "moment_candidate_v2",
                "model_type": "moment_light_adapt",
            },
            "status": "distribution_succeeded",
            "created_at_ns": 1,
            "updated_at_ns": 2,
        }
    )
    ActiveModelVersionStore(settings.database_path).set(
        "moment_light_adapt", "moment_candidate_v2"
    )
    monkeypatch.setattr(service_module, "_moment_runner", None)
    monkeypatch.setattr(service_module, "_moment_runner_settings", None)

    runner = service_module.preload_moment_runner(
        settings, runner_factory=_FakeRunner
    )

    assert runner.loaded is True
    assert runner.model_version == "moment_candidate_v2"
    assert runner.settings.moment_checkpoint_path.name == "best_model.pt"
    assert runner.settings.moment_condition_norm_path.name == "condition_norm.json"
