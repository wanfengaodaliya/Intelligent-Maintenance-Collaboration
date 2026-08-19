import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.post_validator import (
    select_post_validation_metrics,
    validate_post_deployment,
)
from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from cloud_service.storage.database import connect


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


class FailingTrainingDataSource:
    def load(self, update):
        raise ValueError("SOURCE_UNAVAILABLE")


class ChangingLabelProvider:
    """Return a different label if the same packet is resolved twice."""

    def __init__(self):
        self.calls = {}

    def confirm(self, sample):
        packet_id = sample["packet_id"]
        count = self.calls.get(packet_id, 0)
        self.calls[packet_id] = count + 1
        return {
            "packet_id": packet_id,
            "confirmed_label": "fault" if count == 0 else "normal",
            "label_source": "cloud_reference",
        }


def _analysis(problem, *, analysis_id="analysis_001", underestimation=0.20):
    packet_analysis = {
        "status": "succeeded",
        "reviewed_packet_count": 20,
        "cloud_correction_rate": underestimation,
        "risk_underestimation_rate": underestimation,
        "risk_overestimation_rate": 0.02,
    }
    if problem.get("problem_context"):
        packet_analysis["condition_metrics"] = [
            {
                **problem["problem_context"],
                "reviewed_packet_count": 20,
                "cloud_correction_rate": underestimation,
                "risk_underestimation_rate": underestimation,
                "risk_overestimation_rate": 0.02,
            }
        ]
    return {
        "analysis_id": analysis_id,
        "schema_version": "global_analysis_result/2.0",
        "scenario_type": "bearing",
        "subject_id": "machine_01",
        "problem_candidates": [problem],
        "packet_diagnosis_analysis": packet_analysis,
    }


def _problem(**changes):
    value = {
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


def _service(tmp_path: Path):
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


def test_full_manual_lifecycle_hands_off_contract_without_downloading(tmp_path: Path):
    service = _service(tmp_path)
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
            "feature_pipeline_version": "edge_feature_v1",
        }
    )["update"]
    prepared = service.prepare_data(created["update_id"])
    training = service.start_training(created["update_id"])
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"candidate")
    trained = service.register_training_result(
        created["update_id"],
        {
            "candidate_version": "edge_v2",
            "artifact_path": "candidate.bin",
            "artifact_sha256": hashlib.sha256(b"candidate").hexdigest(),
            "model_type": "generic",
            "feature_pipeline_version": "edge_feature_v1",
            "input_feature_schema": {
                "vibration.kurtosis": "number",
                "vibration.rms": "number",
            },
            "training_dataset_id": prepared["training_dataset_id"],
            "training_config": {},
            "training_metrics": {},
        },
    )
    manifest = service.dataset_repository.get_by_update(created["update_id"])
    validated = service.validate(
        created["update_id"], _validation_results(manifest)
    )
    approved = service.approve(created["update_id"], confirmed_by="operator")
    handed_off = service.handoff_distribution(created["update_id"])

    assert prepared["status"] == "waiting_training"
    assert training["status"] == "training"
    assert trained["status"] == "trained"
    assert validated["status"] == "waiting_confirmation"
    assert approved["status"] == "approved"
    assert handed_off["status"] == "handoff_to_distribution"
    assert "download_url" not in handed_off["distribution_result"]
    assert set(handed_off["distribution_result"]) >= {
        "artifact_path",
        "artifact_sha256",
        "input_feature_schema",
    }
    distributed = service.record_distribution_result(
        created["update_id"], {"status": "succeeded", "deployment_id": "deploy_001"}
    )
    _save_analysis(
        service.database_path,
        _analysis(
            _problem(), analysis_id="analysis_old", underestimation=0.05
        ),
        created_at_ns=1,
    )
    with pytest.raises(ModelUpdateError, match="POST_ANALYSIS_NOT_AFTER_DISTRIBUTION"):
        service.post_validate(created["update_id"], "analysis_old")
    _save_analysis(
        service.database_path,
        _analysis(
            _problem(), analysis_id="analysis_002", underestimation=0.05
        ),
        created_at_ns=time.time_ns(),
    )
    verified = service.post_validate(created["update_id"], "analysis_002")
    assert distributed["status"] == "distribution_succeeded"
    assert verified["status"] == "succeeded"


def test_observed_problem_does_not_create_task(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    service = ModelUpdateService(database_path, data_root=tmp_path)
    _save_analysis(database_path, _analysis(_problem(persistence="temporary")))

    result = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )

    assert result == {"decision": "observe", "update": None}


def test_data_source_failure_is_persisted_as_data_prepare_failed(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=FailingTrainingDataSource(),
        label_provider=LabelConfirmationResolver([CloudReferenceProvider()]),
    )
    _save_analysis(database_path, _analysis(_problem()))
    update = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]

    with pytest.raises(ModelUpdateError, match="SOURCE_UNAVAILABLE"):
        service.prepare_data(update["update_id"])

    assert service.get(update["update_id"])["status"] == "data_prepare_failed"


def test_prepare_data_retries_after_exit_in_data_preparing_state(tmp_path: Path):
    service = _service(tmp_path)
    update = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    service.repository.update(update["update_id"], status="data_preparing")

    recovered = service.prepare_data(update["update_id"])

    assert recovered["status"] == "waiting_training"
    assert recovered["training_dataset_id"]


def test_prepare_data_finalizes_task_when_manifest_was_already_saved(tmp_path: Path):
    service = _service(tmp_path)
    update = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    prepared = service.prepare_data(update["update_id"])
    service.repository.update(
        update["update_id"], status="data_preparing", training_dataset_id=None
    )

    recovered = service.prepare_data(update["update_id"])

    assert recovered["status"] == "waiting_training"
    assert recovered["training_dataset_id"] == prepared["training_dataset_id"]


def test_prepare_data_persists_the_same_label_snapshot_used_by_manifest(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    provider = ChangingLabelProvider()
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(),
        label_provider=provider,
    )
    _save_analysis(database_path, _analysis(_problem()))
    update = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]

    service.prepare_data(update["update_id"])
    manifest = service.dataset_repository.get_by_update(update["update_id"])

    for sample_id, label in manifest["sample_labels"].items():
        packet_id = sample_id.replace("sample_", "packet_")
        assert provider.calls[packet_id] == 1
        assert service.label_repository.get(packet_id)["confirmed_label"] == label[
            "confirmed_label"
        ]


def test_training_result_cannot_bypass_explicit_training_start(tmp_path: Path):
    service = _service(tmp_path)
    update = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    prepared = service.prepare_data(update["update_id"])

    with pytest.raises(ModelUpdateError, match="INVALID_UPDATE_STATE"):
        service.register_training_result(
            update["update_id"],
            {"training_dataset_id": prepared["training_dataset_id"]},
        )


@pytest.mark.parametrize(
    ("new_metrics", "expected"),
    [
        (
            {"risk_underestimation_rate": 0.05, "risk_overestimation_rate": 0.02, "cloud_correction_rate": 0.08},
            "succeeded",
        ),
        (
            {"risk_underestimation_rate": 0.19, "risk_overestimation_rate": 0.02, "cloud_correction_rate": 0.19},
            "ineffective",
        ),
        (
            {"risk_underestimation_rate": 0.05, "risk_overestimation_rate": 0.10, "cloud_correction_rate": 0.22},
            "partial_improvement",
        ),
    ],
)
def test_post_validation_distinguishes_outcomes(new_metrics, expected):
    task = {
        "problem_type": "risk_underestimation",
        "evidence_snapshot": {
            "risk_underestimation_rate": 0.20,
            "risk_overestimation_rate": 0.02,
            "cloud_correction_rate": 0.20,
        },
    }

    assert validate_post_deployment(task, new_metrics)["outcome"] == expected


def test_post_validation_rejects_insufficient_global_analysis():
    with pytest.raises(ValueError, match="POST_ANALYSIS_NOT_READY"):
        select_post_validation_metrics(
            {
                "packet_diagnosis_analysis": {
                    "status": "insufficient_data",
                    "reviewed_packet_count": 3,
                }
            },
            problem_context={},
            minimum_sample_count=20,
        )


def test_contextual_post_validation_requires_matching_condition_metrics():
    packet_analysis = {
        "status": "succeeded",
        "reviewed_packet_count": 20,
        "risk_underestimation_rate": 0.01,
        "risk_overestimation_rate": 0.01,
        "cloud_correction_rate": 0.02,
        "condition_metrics": [
            {
                "operating_condition": "low_load",
                "reviewed_packet_count": 20,
                "risk_underestimation_rate": 0.01,
                "risk_overestimation_rate": 0.01,
                "cloud_correction_rate": 0.02,
            }
        ],
    }

    with pytest.raises(ValueError, match="POST_CONTEXT_METRICS_NOT_FOUND"):
        select_post_validation_metrics(
            {"packet_diagnosis_analysis": packet_analysis},
            problem_context={"operating_condition": "high_load"},
            minimum_sample_count=20,
        )


def test_rollback_request_records_baseline_without_deploying(tmp_path: Path):
    service = _service(tmp_path)
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]
    service.repository.update(created["update_id"], status="ineffective")

    task = service.request_rollback(created["update_id"], requested_by="operator")

    assert task["rollback_requested"] is True
    assert task["rollback_target_version"] == "edge_v1"
    assert "deployment" not in task


def test_approval_is_forbidden_before_validation_passes(tmp_path: Path):
    service = _service(tmp_path)
    created = service.create(
        {
            "analysis_id": "analysis_001",
            "problem_id": "problem_001",
            "baseline_version": "edge_v1",
        }
    )["update"]

    with pytest.raises(ModelUpdateError, match="INVALID_UPDATE_STATE"):
        service.approve(created["update_id"], confirmed_by="operator")


def test_api_exposes_final_lifecycle_routes_without_model_download():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cloud_service.app import app; "
            "print('\\n'.join(sorted({route.path for route in app.routes})))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    paths = set(completed.stdout.splitlines())
    assert "/cloud/model-update/{update_id}/prepare-data" in paths
    assert "/cloud/model-update/{update_id}/training-result" in paths
    assert "/cloud/model-update/{update_id}/start-training" in paths
    assert "/cloud/model-update/{update_id}/handoff-distribution" in paths
    assert "/cloud/model-update/{update_id}/post-validate" in paths
    assert "/cloud/model-update/{update_id}/file" not in paths
