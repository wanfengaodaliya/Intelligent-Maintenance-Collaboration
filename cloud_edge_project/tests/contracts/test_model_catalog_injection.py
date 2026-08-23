import json
from pathlib import Path

from cloud_service.model_update.distribution_client import build_distribution_request
from cloud_service.model_update.service import ModelUpdateService
from cloud_service.storage.database import connect
from core.model_lifecycle import ModelCatalog, ModelDescriptor


GENERIC_CATALOG = ModelCatalog(
    scenario_id="pump",
    default_model_id="pump_edge",
    models={
        "pump_edge": ModelDescriptor(
            model_id="pump_edge",
            family="edge",
            default_version="pump-v1",
            description="pump edge model",
        )
    },
)


def _approved() -> dict:
    return {
        "update_id": "update_pump",
        "baseline_version": "pump-v1",
        "candidate_version": "pump-v2",
        "artifact_path": "/data/pump-v2",
        "artifact_sha256": "abc",
        "model_type": "pump_edge",
        "feature_pipeline_version": "pump-features-v1",
        "input_feature_schema": {"pressure": "number"},
    }


def _save_analysis(database_path: Path) -> None:
    result = {
        "problem_candidates": [
            {
                "problem_id": "problem_pump",
                "problem_layer": "packet_diagnosis",
                "problem_type": "risk_underestimation",
                "problem_context": {},
                "evidence": {"sample_count": 20, "cloud_correction_rate": 0.2},
                "persistence": "persistent",
                "suggested_action": "model_update",
            }
        ]
    }
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
                   analysis_id,scenario_type,subject_id,task_count,
                   reviewed_packet_count,cloud_correction_rate,result_json,created_at_ns
               ) VALUES (?,?,?,?,?,?,?,?)""",
            ("analysis_pump", "pump", "pump_01", 20, 20, 0.2, json.dumps(result), 1),
        )


def test_distribution_uses_injected_non_bearing_catalog() -> None:
    request = build_distribution_request(
        _approved(),
        subject_id="pump_01",
        model_catalog=GENERIC_CATALOG,
    )

    assert request["model_family"] == "edge"
    assert request["target"]["scope_subject_id"] == "pump_01"


def test_model_update_service_uses_injected_default_and_seed_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cloud.db"
    service = ModelUpdateService(database_path, model_catalog=GENERIC_CATALOG)
    _save_analysis(database_path)

    created = service.create(
        {"analysis_id": "analysis_pump", "problem_id": "problem_pump"}
    )

    assert created["update"]["model_type"] == "pump_edge"
    assert created["update"]["baseline_version"] == "pump-v1"
