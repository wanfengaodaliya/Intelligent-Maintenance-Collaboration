from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.model_types import (
    ActiveModelVersionStore,
    MODEL_TYPE_SPECS,
    validate_model_type,
)
from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from cloud_service.storage.database import connect


class StaticTrainingDataSource:
    def load(self, update):
        return [
            {
                "sample_id": f"sample_{index}", "packet_id": f"packet_{index}",
                "task_id": f"task_{group}", "source_file": f"{group}.mat",
                "features": {"vibration": {"rms": float(index), "kurtosis": 3.0}},
                "historical_edge_result": {"label": "normal"},
                "cloud_label": "fault", "is_cloud_reviewed": True,
            }
            for index, group in enumerate(("a", "b", "c"), 1)
        ]


def _analysis(problem, *, analysis_id="analysis_001"):
    return {
        "analysis_id": analysis_id,
        "schema_version": "global_analysis_result/2.0",
        "scenario_type": "bearing",
        "subject_id": "machine_01",
        "problem_candidates": [problem],
        "packet_diagnosis_analysis": {
            "status": "succeeded", "reviewed_packet_count": 20,
            "cloud_correction_rate": 0.20, "risk_underestimation_rate": 0.20,
            "risk_overestimation_rate": 0.02,
        },
    }


def _problem(**changes):
    value = {
        "problem_id": "problem_001", "problem_layer": "packet_diagnosis",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "evidence": {"sample_count": 20, "cloud_correction_rate": 0.2},
        "persistence": "persistent", "suggested_action": "model_update",
    }
    value.update(changes)
    return value


def _save_analysis(database_path: Path, result):
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
                   analysis_id,scenario_type,subject_id,task_count,
                   reviewed_packet_count,cloud_correction_rate,result_json,created_at_ns
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                result["analysis_id"], result["scenario_type"], result["subject_id"],
                20, 20, 0.20, json.dumps(result), 1,
            ),
        )


def _service(tmp_path: Path) -> ModelUpdateService:
    database_path = tmp_path / "cloud.db"
    return ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(),
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
    )


def test_model_type_vocabulary_covers_both_models():
    assert set(MODEL_TYPE_SPECS) == {"distilled_h5", "moment_light_adapt"}
    assert MODEL_TYPE_SPECS["distilled_h5"].family == "edge"
    assert MODEL_TYPE_SPECS["moment_light_adapt"].family == "cloud"


def test_validate_model_type_rejects_unknown():
    with pytest.raises(ValueError):
        validate_model_type("mystery_model")


def test_active_version_store_roundtrip(tmp_path: Path):
    store = ActiveModelVersionStore(tmp_path / "cloud.db")
    assert store.get("distilled_h5") is None
    store.set("distilled_h5", "distilled_h5_v2")
    assert store.get("distilled_h5") == "distilled_h5_v2"
    store.set("distilled_h5", "distilled_h5_v3")
    assert store.get("distilled_h5") == "distilled_h5_v3"


def test_create_defaults_baseline_to_model_seed_version(tmp_path: Path):
    service = _service(tmp_path)
    _save_analysis(service.database_path, _analysis(_problem()))
    created = service.create(
        {"analysis_id": "analysis_001", "problem_id": "problem_001", "model_type": "distilled_h5"}
    )
    assert created["update"]["model_type"] == "distilled_h5"
    assert created["update"]["baseline_version"] == "distilled_h5_kd_fold3_a9f20442"


def test_create_uses_active_version_as_baseline(tmp_path: Path):
    service = _service(tmp_path)
    service._active_versions().set("moment_light_adapt", "moment-retrained-v1")
    _save_analysis(service.database_path, _analysis(_problem()))
    created = service.create(
        {"analysis_id": "analysis_001", "problem_id": "problem_001", "model_type": "moment_light_adapt"}
    )
    assert created["update"]["model_type"] == "moment_light_adapt"
    assert created["update"]["baseline_version"] == "moment-retrained-v1"


def test_create_rejects_unknown_model_type(tmp_path: Path):
    service = _service(tmp_path)
    _save_analysis(service.database_path, _analysis(_problem()))
    with pytest.raises(ModelUpdateError):
        service.create(
            {"analysis_id": "analysis_001", "problem_id": "problem_001", "model_type": "mystery_model"}
        )


def test_create_keeps_explicit_baseline_when_provided(tmp_path: Path):
    service = _service(tmp_path)
    _save_analysis(service.database_path, _analysis(_problem()))
    created = service.create(
        {
            "analysis_id": "analysis_001", "problem_id": "problem_001",
            "model_type": "distilled_h5", "baseline_version": "distilled_h5_now",
        }
    )
    assert created["update"]["baseline_version"] == "distilled_h5_now"