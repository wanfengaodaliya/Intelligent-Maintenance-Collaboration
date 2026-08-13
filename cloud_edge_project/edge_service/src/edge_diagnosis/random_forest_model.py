"""Local packet-level diagnosis backed by the committed random forest."""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from edge_model.code_fallback import CodeFallbackRunner
from edge_model.contracts import EdgeResult, PacketInferenceTask

from .feature_adapter import feature_vector


RUNTIME_MODEL_VERSION = "bearing-rf-a2-evaluation-v1"
DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[2] / "models" / "bearing_random_forest"
)


class ModelArtifactError(RuntimeError):
    pass


class RandomForestDiagnosticModel(CodeFallbackRunner):
    """Run the frozen normal/fault classifier using the existing EdgeResult contract."""

    def __init__(self, model_dir: Path | str = DEFAULT_MODEL_DIR):
        root = Path(model_dir).resolve()
        manifest_path = root / "model_manifest.json"
        model_path = root / "random_forest.joblib"
        schema_path = root / "feature_schema.json"
        label_path = root / "label_mapping.json"
        try:
            manifest = _read_json(manifest_path)
            schema = _read_json(schema_path)
            labels = _read_json(label_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ModelArtifactError("invalid random-forest metadata") from exc

        _require_digest(model_path, manifest.get("model_sha256"), "model")
        _require_metadata_digest(
            schema_path, manifest.get("feature_schema_sha256"), "feature schema"
        )
        _require_metadata_digest(
            label_path, manifest.get("label_mapping_sha256"), "label mapping"
        )
        if manifest.get("model_version") != RUNTIME_MODEL_VERSION:
            raise ModelArtifactError("unexpected random-forest model version")
        if manifest.get("task") != "binary_fault_detection":
            raise ModelArtifactError("unexpected random-forest task")

        schema_columns = tuple(item["name"] for item in schema.get("features", ()))
        manifest_columns = tuple(manifest.get("feature_columns", ()))
        if not schema_columns or manifest_columns != schema_columns:
            raise ModelArtifactError("feature schema does not match model manifest")
        if labels.get("labels") != {"normal": 0, "fault": 1}:
            raise ModelArtifactError("label mapping is not the frozen binary contract")

        feature_schema_version = schema.get("schema_version")
        model_input_schema_version = schema.get("model_input_schema_version")
        if not isinstance(feature_schema_version, str) or not feature_schema_version:
            raise ModelArtifactError("feature schema version is missing")
        if not isinstance(model_input_schema_version, str) or not model_input_schema_version:
            raise ModelArtifactError("model input schema version is missing")

        try:
            artifact = joblib.load(model_path)
        except Exception as exc:  # joblib may expose backend-specific exceptions
            raise ModelArtifactError("random-forest model cannot be loaded") from exc
        if not isinstance(artifact, dict):
            raise ModelArtifactError("random-forest artifact must be an object")
        if tuple(artifact.get("feature_columns", ())) != schema_columns:
            raise ModelArtifactError("artifact feature order does not match schema")
        estimator = artifact.get("estimator")
        if estimator is None or not callable(getattr(estimator, "predict_proba", None)):
            raise ModelArtifactError("artifact does not contain a probability classifier")
        classes = tuple(str(value) for value in getattr(estimator, "classes_", ()))
        if set(classes) != {"normal", "fault"}:
            raise ModelArtifactError("classifier classes are not normal/fault")

        self.rule_version = RUNTIME_MODEL_VERSION
        self.model_version = RUNTIME_MODEL_VERSION
        self.deployment_status = str(manifest.get("deployment_status"))
        self.feature_columns = schema_columns
        self.feature_schema_version = feature_schema_version
        self.model_input_schema_version = model_input_schema_version
        self.estimator = estimator
        self.classes = classes

    def run(self, task: PacketInferenceTask) -> EdgeResult:
        self._validate_input(task)
        vector = feature_vector(task.perception, self.feature_columns)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="X does not have valid feature names",
                category=UserWarning,
            )
            probabilities = np.asarray(self.estimator.predict_proba(vector)[0])
        if probabilities.shape != (len(self.classes),) or not np.isfinite(
            probabilities
        ).all():
            raise ValueError("random-forest probabilities are invalid")
        index = int(np.argmax(probabilities))
        label = self.classes[index]
        result = EdgeResult(
            edge_result=label,
            confidence=round(float(probabilities[index]), 6),
            edge_risk_level="high" if label == "fault" else "low",
            model_version=self.model_version,
        )
        self._validate_output(result)
        return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_digest(path: Path, expected: object, description: str) -> None:
    try:
        actual = _sha256(path)
    except OSError as exc:
        raise ModelArtifactError("%s file is missing" % description) from exc
    if not isinstance(expected, str) or actual != expected.lower():
        raise ModelArtifactError("%s checksum mismatch" % description)


def _require_metadata_digest(path: Path, expected: object, description: str) -> None:
    try:
        actual = _sha256_normalized_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise ModelArtifactError("%s file is missing" % description) from exc
    if not isinstance(expected, str) or actual != expected.lower():
        raise ModelArtifactError("%s checksum mismatch" % description)


def _sha256_normalized_text(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    digest = hashlib.sha256()
    digest.update(content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    return digest.hexdigest()
