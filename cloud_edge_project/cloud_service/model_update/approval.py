"""Human approval gate and standard ApprovedModel construction."""

from __future__ import annotations

import time
from typing import Any


class ApprovalError(ValueError):
    pass


def approve_candidate(task: dict[str, Any], approved_by: str) -> dict[str, Any]:
    validation = task.get("validation_result")
    if not isinstance(validation, dict) or validation.get("validation_passed") is not True:
        raise ApprovalError("VALIDATION_NOT_PASSED")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ApprovalError("APPROVER_REQUIRED")
    artifact = task.get("candidate_artifact")
    if not isinstance(artifact, dict):
        raise ApprovalError("CANDIDATE_NOT_REGISTERED")
    return {
        "update_id": task["update_id"],
        "baseline_version": task["baseline_version"],
        "candidate_version": task["candidate_version"],
        "artifact_path": artifact["artifact_path"],
        "artifact_sha256": artifact["artifact_sha256"],
        "model_type": artifact["model_type"],
        "feature_pipeline_version": artifact["feature_pipeline_version"],
        "input_feature_schema": artifact["input_feature_schema"],
        "training_dataset_id": artifact["training_dataset_id"],
        "approved_by": approved_by.strip(),
        "approved_at_ns": time.time_ns(),
    }
