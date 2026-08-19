from __future__ import annotations

from pathlib import Path

import pytest

from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from cloud_service.model_update.trainer import (
    TRAINER_REGISTRY,
    build_training_plan,
    resolve_trainer,
)
from cloud_service.storage.database import initialize_database


def _save_analysis(database_path: Path) -> None:
    import json

    from cloud_service.storage.database import connect

    result = {
        "analysis_id": "analysis_trainer",
        "scenario_type": "bearing",
        "subject_id": "bearing_01",
        "problem_candidates": [
            {
                "problem_id": "problem_trainer",
                "problem_layer": "device_arbitration",
                "problem_type": "high_conflict_rate_model",
                "severity": "medium",
                "persistence": "persistent",
                "evidence": {"sample_count": 30},
                "suggested_action": "model_update",
            }
        ],
    }
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


def _service(tmp_path: Path, model_type: str = "distilled_h5"):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    _save_analysis(database_path)
    from cloud_service.model_update.label_confirmation import (
        CloudReferenceProvider,
        LabelConfirmationResolver,
    )

    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(),
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
    )
    update = service.create(
        {
            "analysis_id": "analysis_trainer",
            "problem_id": "problem_trainer",
            "baseline_version": "edge_v1",
            "model_type": model_type,
        }
    )["update"]
    return service, update


def test_trainer_registry_covers_both_model_families() -> None:
    assert set(TRAINER_REGISTRY) == {"distilled_h5", "moment_light_adapt"}
    assert TRAINER_REGISTRY["distilled_h5"].family == "edge"
    assert TRAINER_REGISTRY["moment_light_adapt"].family == "cloud"


def test_resolve_trainer_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="UNSUPPORTED_MODEL_TYPE"):
        resolve_trainer("generic")


def test_build_training_plan_is_deterministic_per_update(tmp_path: Path) -> None:
    first = build_training_plan(
        update_id="update_x",
        model_type="distilled_h5",
        dataset_id="dataset_1",
        training_root=tmp_path,
    )
    second = build_training_plan(
        update_id="update_x",
        model_type="distilled_h5",
        dataset_id="dataset_1",
        training_root=tmp_path,
    )

    assert first == second
    assert first["trainer_id"] == "distilled_h5_kd_trainer"
    assert first["output_dir"] == str(tmp_path / "distilled_h5" / "update_x")


def test_start_training_persists_edge_trainer_plan(tmp_path: Path) -> None:
    service, update = _service(tmp_path)
    prepared = service.prepare_data(update["update_id"])

    training = service.start_training(update["update_id"])

    assert training["status"] == "training"
    plan = training["trainer_plan"]
    assert plan["model_type"] == "distilled_h5"
    assert plan["model_family"] == "edge"
    assert plan["dataset_id"] == prepared["training_dataset_id"]
    assert plan["output_dir"].startswith(str(tmp_path))


def test_start_training_persists_cloud_trainer_plan(tmp_path: Path) -> None:
    service, update = _service(tmp_path, model_type="moment_light_adapt")
    service.prepare_data(update["update_id"])

    training = service.start_training(update["update_id"])

    plan = training["trainer_plan"]
    assert plan["model_type"] == "moment_light_adapt"
    assert plan["model_family"] == "cloud"
    assert plan["trainer_id"] == "moment_light_adapt_trainer"