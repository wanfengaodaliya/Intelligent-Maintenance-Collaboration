import hashlib
from pathlib import Path

import pytest

from cloud_service.model_update.approval import ApprovalError, approve_candidate
from cloud_service.model_update.candidate_registry import CandidateRegistry
from cloud_service.model_update.contracts import ModelUpdateConfig
from cloud_service.model_update.validator import validate_candidate


def _update():
    return {
        "update_id": "update_001",
        "baseline_version": "edge_v1",
        "candidate_version": "edge_v2",
        "problem_type": "risk_underestimation",
        "problem_context": {"operating_condition": "high_load"},
        "candidate_artifact": {
            "artifact_path": "candidate.bin",
            "artifact_sha256": "abc",
            "model_type": "generic",
            "feature_pipeline_version": "edge_feature_v1",
            "input_feature_schema": {"vibration.rms": "number"},
            "training_dataset_id": "dataset_001",
        },
    }


def _manifest():
    return {
        "dataset_id": "dataset_001",
        "feature_pipeline_version": "edge_feature_v1",
        "input_feature_schema": {"vibration.rms": "number"},
        "test_sample_ids": ["sample_1", "sample_2", "sample_3"],
        "focus_sample_ids": ["sample_1", "sample_2", "sample_3"],
        "sample_labels": {
            "sample_1": {"confirmed_label": "fault", "label_source": "dataset_ground_truth"},
            "sample_2": {"confirmed_label": "fault", "label_source": "dataset_ground_truth"},
            "sample_3": {"confirmed_label": "normal", "label_source": "dataset_ground_truth"},
        },
    }


def _result(sample_id: str, truth: str, baseline: str, candidate: str):
    return {
        "sample_id": sample_id,
        "confirmed_label": truth,
        "label_source": "dataset_ground_truth",
        "baseline_prediction": baseline,
        "candidate_prediction": candidate,
        "problem_context": {"operating_condition": "high_load"},
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_payload(artifact_dir: Path, files: dict[str, bytes], primary: str) -> dict:
    for name, data in files.items():
        (artifact_dir / name).write_bytes(data)
    return {
        "candidate_version": "edge_v2",
        "artifact_path": str(artifact_dir),
        "artifact_sha256": _sha256_bytes(files[primary]),
        "artifact_bundle": [
            {"rel_path": name, "sha256": _sha256_bytes(data)}
            for name, data in files.items()
        ],
        "model_type": "distilled_h5",
        "feature_pipeline_version": "edge_feature_v1",
        "input_feature_schema": {"vibration.rms": "number"},
        "training_dataset_id": "dataset_001",
    }


def test_candidate_registry_accepts_multi_file_bundle(tmp_path: Path):
    registry = CandidateRegistry(tmp_path)
    artifact_dir = tmp_path / "candidate_v2"
    artifact_dir.mkdir()
    payload = _bundle_payload(
        artifact_dir,
        {
            "best_model.pt": b"pt-bytes",
            "condition_norm.json": b'{"mean": [0.0]}',
            "model_metadata.json": b'{"version": "v2"}',
        },
        primary="best_model.pt",
    )

    candidate = registry.register(_manifest(), payload)

    assert candidate["artifact_bundle"]["entries"][0]["rel_path"] == "best_model.pt"
    assert candidate["artifact_bundle"]["artifact_sha256"] == _sha256_bytes(b"pt-bytes")
    assert candidate["model_type"] == "distilled_h5"


def test_candidate_registry_rejects_bundle_with_tampered_file(tmp_path: Path):
    registry = CandidateRegistry(tmp_path)
    artifact_dir = tmp_path / "candidate_v2"
    artifact_dir.mkdir()
    payload = _bundle_payload(
        artifact_dir,
        {"best_model.pt": b"pt-bytes", "condition_norm.json": b'{"mean": [0.0]}'},
        primary="best_model.pt",
    )
    payload["artifact_bundle"][1]["sha256"] = "0" * 64

    with pytest.raises(ValueError) as error:
        registry.register(_manifest(), payload)
    assert "BUNDLE_ARTIFACT_SHA256_MISMATCH" in str(error.value)


def test_candidate_registry_requires_primary_in_bundle(tmp_path: Path):
    registry = CandidateRegistry(tmp_path)
    artifact_dir = tmp_path / "candidate_v2"
    artifact_dir.mkdir()
    payload = _bundle_payload(
        artifact_dir,
        {"best_model.pt": b"pt-bytes", "condition_norm.json": b'{"mean": [0.0]}'},
        primary="best_model.pt",
    )
    payload["artifact_bundle"] = payload["artifact_bundle"][1:]

    with pytest.raises(ValueError) as error:
        registry.register(_manifest(), payload)
    assert "BUNDLE_MISSING_PRIMARY_ARTIFACT" in str(error.value)


def test_candidate_registry_keeps_single_file_compatibility(tmp_path: Path):
    registry = CandidateRegistry(tmp_path)
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"single")
    payload = {
        "candidate_version": "edge_v2",
        "artifact_path": "candidate.bin",
        "artifact_sha256": _sha256_bytes(b"single"),
        "model_type": "generic",
        "feature_pipeline_version": "edge_feature_v1",
        "input_feature_schema": {"vibration.rms": "number"},
        "training_dataset_id": "dataset_001",
    }

    candidate = registry.register(_manifest(), payload)

    assert candidate["artifact_bundle"] is None
    assert candidate["artifact_sha256"] == _sha256_bytes(b"single")


def test_worse_candidate_fails_hard_gate_and_cannot_be_approved():
    results = [
        _result("sample_1", "fault", "fault", "warning"),
        _result("sample_2", "fault", "warning", "normal"),
        _result("sample_3", "normal", "normal", "normal"),
    ]

    validation = validate_candidate(
        _update(), _manifest(), results, ModelUpdateConfig()
    )

    assert validation["validation_passed"] is False
    assert validation["target_metric"] == "risk_underestimation_rate"
    with pytest.raises(ApprovalError, match="VALIDATION_NOT_PASSED"):
        approve_candidate({**_update(), "validation_result": validation}, "operator")


def test_candidate_that_fixes_target_without_overall_regression_can_be_approved():
    results = [
        _result("sample_1", "fault", "warning", "fault"),
        _result("sample_2", "fault", "normal", "fault"),
        _result("sample_3", "normal", "normal", "normal"),
    ]

    validation = validate_candidate(
        _update(), _manifest(), results, ModelUpdateConfig()
    )
    approved = approve_candidate(
        {**_update(), "validation_result": validation}, "operator"
    )

    assert validation["validation_passed"] is True
    assert approved["update_id"] == "update_001"
    assert approved["artifact_sha256"] == "abc"
    assert approved["approved_by"] == "operator"


def test_validation_requires_the_exact_frozen_test_sample_ids():
    results = [
        _result("sample_1", "fault", "warning", "fault"),
        _result("sample_2", "fault", "warning", "fault"),
    ]

    with pytest.raises(ValueError, match="FROZEN_TEST_SET_MISMATCH"):
        validate_candidate(_update(), _manifest(), results, ModelUpdateConfig())


def test_candidate_registry_records_sha_and_feature_contract(tmp_path: Path):
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"candidate-model")
    expected_sha = hashlib.sha256(b"candidate-model").hexdigest()

    registered = CandidateRegistry(tmp_path).register(
        _manifest(),
        {
            "candidate_version": "edge_v2",
            "artifact_path": "candidate.bin",
            "artifact_sha256": expected_sha,
            "model_type": "generic",
            "feature_pipeline_version": "edge_feature_v1",
            "input_feature_schema": {"vibration.rms": "number"},
            "training_dataset_id": "dataset_001",
            "training_config": {"seed": 7},
            "training_metrics": {"validation_f1": 0.9},
        },
    )

    assert registered["artifact_sha256"] == expected_sha
    assert registered["training_dataset_id"] == "dataset_001"
    assert registered["feature_pipeline_version"] == "edge_feature_v1"


def test_candidate_registry_rejects_features_outside_frozen_edge_contract(
    tmp_path: Path,
):
    artifact = tmp_path / "candidate.bin"
    artifact.write_bytes(b"candidate-model")

    with pytest.raises(ValueError, match="CANDIDATE_INPUT_SCHEMA_INCOMPATIBLE"):
        CandidateRegistry(tmp_path).register(
            _manifest(),
            {
                "candidate_version": "edge_v2",
                "artifact_path": "candidate.bin",
                "artifact_sha256": hashlib.sha256(b"candidate-model").hexdigest(),
                "model_type": "generic",
                "feature_pipeline_version": "edge_feature_v1",
                "input_feature_schema": {"cloud.envelope_peak": "number"},
                "training_dataset_id": "dataset_001",
            },
        )


def test_detailed_fault_labels_use_explicit_risk_levels_for_target_metric():
    results = [
        {
            "sample_id": sample_id,
            "confirmed_label": "outer_ring_damage",
            "confirmed_risk_level": "fault",
            "label_source": "dataset_ground_truth",
            "baseline_prediction": "healthy",
            "baseline_risk_level": "normal",
            "candidate_prediction": "outer_ring_damage",
            "candidate_risk_level": "fault",
            "problem_context": {"operating_condition": "high_load"},
        }
        for sample_id in _manifest()["test_sample_ids"]
    ]

    manifest = _manifest()
    manifest["sample_labels"] = {
        sample_id: {
            "confirmed_label": "outer_ring_damage",
            "confirmed_risk_level": "fault",
            "label_source": "dataset_ground_truth",
        }
        for sample_id in manifest["test_sample_ids"]
    }
    validation = validate_candidate(_update(), manifest, results, ModelUpdateConfig())

    assert validation["validation_passed"] is True
    assert validation["candidate_metrics"]["accuracy"] == 1.0
    assert validation["candidate_metrics"]["risk_underestimation_rate"] == 0.0


def test_validation_rejects_labels_that_do_not_match_frozen_manifest():
    results = [
        _result("sample_1", "normal", "normal", "normal"),
        _result("sample_2", "fault", "warning", "fault"),
        _result("sample_3", "normal", "normal", "normal"),
    ]

    with pytest.raises(ValueError, match="FROZEN_LABEL_MISMATCH"):
        validate_candidate(_update(), _manifest(), results, ModelUpdateConfig())


def test_target_metric_uses_manifest_focus_ids_not_caller_context():
    manifest = _manifest()
    manifest["focus_sample_ids"] = ["sample_2"]
    results = [
        _result("sample_1", "fault", "normal", "fault"),
        _result("sample_2", "fault", "fault", "warning"),
        _result("sample_3", "normal", "normal", "normal"),
    ]
    results[1]["problem_context"] = {"operating_condition": "low_load"}

    validation = validate_candidate(
        _update(), manifest, results, ModelUpdateConfig()
    )

    assert validation["validation_passed"] is False
    assert validation["baseline_target_value"] == 0.0
    assert validation["candidate_target_value"] == 1.0
