"""Generic candidate artifact registration without binding a model format."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class CandidateRegistry:
    def __init__(self, artifact_root: Path):
        self.artifact_root = Path(artifact_root).resolve()

    def register(
        self, dataset_manifest: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        required_strings = (
            "candidate_version",
            "artifact_path",
            "artifact_sha256",
            "model_type",
            "feature_pipeline_version",
            "training_dataset_id",
        )
        if any(
            not isinstance(payload.get(key), str) or not payload[key].strip()
            for key in required_strings
        ):
            raise ValueError("INVALID_TRAINING_RESULT")
        if payload["training_dataset_id"] != dataset_manifest["dataset_id"]:
            raise ValueError("TRAINING_DATASET_MISMATCH")
        if (
            payload["feature_pipeline_version"]
            != dataset_manifest["feature_pipeline_version"]
        ):
            raise ValueError("FEATURE_PIPELINE_MISMATCH")
        schema = payload.get("input_feature_schema")
        if not isinstance(schema, dict) or not schema:
            raise ValueError("INPUT_FEATURE_SCHEMA_REQUIRED")
        if not schema_is_compatible(
            dataset_manifest.get("input_feature_schema"), schema
        ):
            raise ValueError("CANDIDATE_INPUT_SCHEMA_INCOMPATIBLE")
        supplied = Path(payload["artifact_path"])
        artifact = (
            supplied.resolve()
            if supplied.is_absolute()
            else (self.artifact_root / supplied).resolve()
        )
        bundle = payload.get("artifact_bundle")
        if bundle is not None:
            resolved_bundle = self._resolve_bundle(
                artifact, bundle, payload["artifact_sha256"]
            )
            digest = resolved_bundle["artifact_sha256"]
        else:
            if self.artifact_root not in artifact.parents or not artifact.is_file():
                raise ValueError("CANDIDATE_ARTIFACT_NOT_FOUND")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if digest != payload["artifact_sha256"]:
                raise ValueError("CANDIDATE_ARTIFACT_SHA256_MISMATCH")
            resolved_bundle = None
        return {
            "candidate_version": payload["candidate_version"].strip(),
            "artifact_path": str(artifact),
            "artifact_sha256": digest,
            "artifact_bundle": resolved_bundle,
            "model_type": payload["model_type"].strip(),
            "feature_pipeline_version": payload["feature_pipeline_version"].strip(),
            "input_feature_schema": dict(schema),
            "training_dataset_id": payload["training_dataset_id"].strip(),
            "training_config": payload.get("training_config", {}),
            "training_metrics": payload.get("training_metrics", {}),
        }

    def _resolve_bundle(
        self, artifact: Path, bundle: Any, primary_expected: str
    ) -> dict[str, Any]:
        """Validate a multi-file bundle rooted at the artifact directory.

        Each entry is {"rel_path": "...", "sha256": "..."}. The primary
        artifact is the entry whose digest matches the payload's
        artifact_sha256, so the canonical digest stays stable.
        """
        if not isinstance(bundle, list) or not bundle:
            raise ValueError("INVALID_ARTIFACT_BUNDLE")
        if not artifact.is_dir():
            raise ValueError("BUNDLE_ARTIFACT_PATH_MUST_BE_DIRECTORY")
        entries: list[dict[str, Any]] = []
        primary_digest: str | None = None
        for entry in bundle:
            if not isinstance(entry, dict):
                raise ValueError("INVALID_ARTIFACT_BUNDLE")
            rel_path = entry.get("rel_path")
            expected = entry.get("sha256")
            if (
                not isinstance(rel_path, str)
                or not rel_path.strip()
                or not isinstance(expected, str)
                or len(expected) != 64
            ):
                raise ValueError("INVALID_ARTIFACT_BUNDLE")
            resolved = (artifact / rel_path).resolve()
            if artifact not in resolved.parents or not resolved.is_file():
                raise ValueError("BUNDLE_ARTIFACT_NOT_FOUND")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if digest != expected.lower():
                raise ValueError("BUNDLE_ARTIFACT_SHA256_MISMATCH")
            entries.append({"rel_path": rel_path.strip(), "sha256": digest})
            if digest == primary_expected.lower():
                primary_digest = digest
        if primary_digest is None:
            raise ValueError("BUNDLE_MISSING_PRIMARY_ARTIFACT")
        return {"entries": entries, "artifact_sha256": primary_digest}


def schema_is_compatible(frozen: Any, candidate: Any) -> bool:
    """A candidate may use a typed subset of the frozen edge feature contract."""

    return (
        isinstance(frozen, dict)
        and isinstance(candidate, dict)
        and bool(candidate)
        and all(frozen.get(key) == value for key, value in candidate.items())
    )
