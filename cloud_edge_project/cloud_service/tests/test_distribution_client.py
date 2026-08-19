from __future__ import annotations

import pytest

from cloud_service.model_update.distribution_client import (
    build_distribution_request,
    resolve_distribution_target,
)


def _approved(model_type: str) -> dict:
    return {
        "update_id": "update_tgt",
        "baseline_version": "edge_v1",
        "candidate_version": "edge_v2",
        "artifact_path": "/data/candidate_v2",
        "artifact_sha256": "abc",
        "model_type": model_type,
        "feature_pipeline_version": "edge_feature_v1",
        "input_feature_schema": {"vibration.rms": "number"},
        "training_dataset_id": "dataset_1",
    }


def test_edge_target_points_to_edge_node(monkeypatch) -> None:
    target = resolve_distribution_target(
        "distilled_h5", subject_id="bearing_02"
    )

    assert target["family"] == "edge"
    assert target["deploy_to"] == "edge_node"
    assert target["scope_subject_id"] == "bearing_02"
    assert target["edge_node_ids"] == []


def test_cloud_target_points_to_local_cloud_node(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_REVIEW_NODE_ID", "cloud_09")
    target = resolve_distribution_target("moment_light_adapt")

    assert target["family"] == "cloud"
    assert target["deploy_to"] == "local_cloud"
    assert target["cloud_node_id"] == "cloud_09"


def test_build_distribution_request_carries_target_and_family(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_REVIEW_NODE_ID", "cloud_01")
    request = build_distribution_request(
        _approved("distilled_h5"), subject_id="bearing_02"
    )

    assert request["model_family"] == "edge"
    assert request["target"]["scope_subject_id"] == "bearing_02"
    assert set(request) >= {
        "update_id",
        "baseline_version",
        "candidate_version",
        "artifact_path",
        "artifact_sha256",
        "model_type",
        "model_family",
        "target",
    }


def test_cloud_model_distribution_targets_local_cloud(monkeypatch) -> None:
    request = build_distribution_request(_approved("moment_light_adapt"))

    assert request["target"]["deploy_to"] == "local_cloud"


def test_unknown_model_type_rejected() -> None:
    with pytest.raises(ValueError, match="INVALID_APPROVED_MODEL"):
        build_distribution_request(_approved("generic"))