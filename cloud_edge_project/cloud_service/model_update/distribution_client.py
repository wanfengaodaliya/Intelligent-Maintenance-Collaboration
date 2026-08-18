"""Contract-only handoff to the independent model distribution module."""

from __future__ import annotations

from typing import Any


def build_distribution_request(approved_model: dict[str, Any]) -> dict[str, Any]:
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
    return {key: approved_model[key] for key in required}
