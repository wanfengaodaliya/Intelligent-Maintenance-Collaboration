from pathlib import Path
import sqlite3

from cloud_service.model_update.contracts import ModelUpdateConfig
from cloud_service.model_update.decision import decide_update
from cloud_service.model_update.repository import ModelUpdateRepository
from cloud_service.storage.database import connect, initialize_database
from cloud_service.storage.schema import MODEL_UPDATE_TASK_DDL


def _problem(**changes):
    problem = {
        "problem_id": "problem_001",
        "problem_layer": "packet_diagnosis",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "evidence": {"sample_count": 20},
        "persistence": "persistent",
        "suggested_action": "model_update",
    }
    problem.update(changes)
    return problem


def test_temporary_problem_is_observed():
    assert decide_update(
        _problem(persistence="temporary"), ModelUpdateConfig()
    ) == "observe"


def test_persistent_packet_problem_with_enough_evidence_creates_update():
    assert decide_update(
        _problem(), ModelUpdateConfig(min_update_evidence_count=20)
    ) == "create_update"


def test_non_packet_problem_is_observed_even_with_enough_evidence():
    assert decide_update(
        _problem(problem_layer="bearing_aggregation"), ModelUpdateConfig()
    ) == "observe"


def test_repository_schema_does_not_require_candidate_at_task_creation(tmp_path: Path):
    repository = ModelUpdateRepository(tmp_path / "cloud.db")
    with connect(repository.database_path) as connection:
        columns = {
            row["name"]: row for row in connection.execute(
                "PRAGMA table_info(model_update_task)"
            )
        }

    assert columns["candidate_artifact_json"]["notnull"] == 0
    assert columns["candidate_version"]["notnull"] == 0
    assert "problem_id" in columns
    assert "training_dataset_id" in columns
    assert "post_validation_result_json" in columns


def test_v15_candidate_first_task_is_preserved_by_schema_migration(tmp_path: Path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """CREATE TABLE model_update_task (
                   update_id TEXT PRIMARY KEY,analysis_id TEXT NOT NULL,
                   scenario_type TEXT NOT NULL,subject_id TEXT NOT NULL,
                   update_type TEXT NOT NULL,update_reason TEXT NOT NULL,
                   old_version TEXT NOT NULL,new_version TEXT NOT NULL,
                   update_file TEXT NOT NULL,update_file_sha256 TEXT NOT NULL,
                   target_edge_nodes_json TEXT NOT NULL,test_data_limit INTEGER NOT NULL,
                   status TEXT NOT NULL,validation_result_json TEXT,
                   confirmation_json TEXT,distribution_result_json TEXT,
                   created_at_ns INTEGER NOT NULL,updated_at_ns INTEGER NOT NULL
               );
               INSERT INTO model_update_task VALUES (
                   'update_old','analysis_old','bearing','machine_01','model','reason',
                   'edge_v1','edge_v2','candidate.bin','abc','[]',100,'approved',
                   NULL,NULL,NULL,1,2
               );"""
        )

    initialize_database(database_path)
    migrated = ModelUpdateRepository(database_path).get("update_old")

    assert migrated["baseline_version"] == "edge_v1"
    assert migrated["candidate_version"] == "edge_v2"
    assert migrated["candidate_artifact"]["artifact_path"] == "candidate.bin"


def test_v15_migration_recovers_after_schema_creation_was_interrupted(tmp_path: Path):
    database_path = tmp_path / "interrupted.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """CREATE TABLE model_update_task_legacy_v15 (
                   update_id TEXT PRIMARY KEY,analysis_id TEXT NOT NULL,
                   scenario_type TEXT NOT NULL,subject_id TEXT NOT NULL,
                   update_type TEXT NOT NULL,update_reason TEXT NOT NULL,
                   old_version TEXT NOT NULL,new_version TEXT NOT NULL,
                   update_file TEXT NOT NULL,update_file_sha256 TEXT NOT NULL,
                   target_edge_nodes_json TEXT NOT NULL,test_data_limit INTEGER NOT NULL,
                   status TEXT NOT NULL,validation_result_json TEXT,
                   confirmation_json TEXT,distribution_result_json TEXT,
                   created_at_ns INTEGER NOT NULL,updated_at_ns INTEGER NOT NULL
               );
               INSERT INTO model_update_task_legacy_v15 VALUES (
                   'update_interrupted','analysis_old','bearing','machine_01',
                   'model','reason','edge_v1','edge_v2','candidate.bin','abc',
                   '[]',100,'distribution_prepared',NULL,NULL,NULL,1,2
               );"""
        )
        connection.executescript(MODEL_UPDATE_TASK_DDL)

    initialize_database(database_path)
    migrated = ModelUpdateRepository(database_path).get("update_interrupted")

    assert migrated is not None
    assert migrated["status"] == "handoff_to_distribution"
    with sqlite3.connect(database_path) as connection:
        legacy_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='model_update_task_legacy_v15'"
        ).fetchone()
    assert legacy_table is None
