"""Contract-only handoff to the independent model distribution module."""

from __future__ import annotations

import os
from typing import Any

from compatibility.bearing_v12.legacy_exports import BEARING_MODEL_CATALOG
from core.model_lifecycle import ModelCatalog


def resolve_distribution_target(
    model_type: str,
    *,
    subject_id: str | None = None,
    model_catalog: ModelCatalog | None = None,
) -> dict[str, Any]:
    """Describe where a candidate model must be deployed once approved.

    Edge-family models go to an edge node serving the model's subject device;
    cloud-family models stay on the local cloud review node. Edge node ids are
    left to the downstream distribution module to resolve from the subject.
    """

    catalog = model_catalog or BEARING_MODEL_CATALOG
    try:
        descriptor = catalog.require(model_type)
    except ValueError:
        raise ValueError("INVALID_APPROVED_MODEL")
    if descriptor.family not in {"edge", "cloud"}:
        raise ValueError("INVALID_APPROVED_MODEL")
    if descriptor.family == "edge":
        return {
            "family": "edge",
            "deploy_to": "edge_node",
            "scope_subject_id": subject_id,
            "edge_node_ids": [],
        }
    return {
        "family": "cloud",
        "deploy_to": "local_cloud",
        "cloud_node_id": os.getenv("CLOUD_REVIEW_NODE_ID", "cloud_01").strip(),
    }


def build_distribution_request(
    approved_model: dict[str, Any],
    *,
    subject_id: str | None = None,
    model_catalog: ModelCatalog | None = None,
) -> dict[str, Any]:
    required = (
        "update_id",
        "baseline_version",
        "candidate_version",
        "artifact_path",
        "artifact_sha256",
        "feature_pipeline_version",
        "input_feature_schema",
    )
    if any(key not in approved_model for key in required):
        raise ValueError("INVALID_APPROVED_MODEL")
    model_type = approved_model["model_type"]
    catalog = model_catalog or BEARING_MODEL_CATALOG
    try:
        descriptor = catalog.require(model_type)
    except ValueError:
        raise ValueError("INVALID_APPROVED_MODEL")
    if descriptor.family not in {"edge", "cloud"}:
        raise ValueError("INVALID_APPROVED_MODEL")
    result = {key: approved_model[key] for key in required}
    result["model_type"] = model_type
    result["model_family"] = descriptor.family
    result["target"] = resolve_distribution_target(
        model_type,
        subject_id=subject_id,
        model_catalog=catalog,
    )
    return result
