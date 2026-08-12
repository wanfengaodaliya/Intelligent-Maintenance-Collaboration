"""Integration-only 50 ms random-forest diagnostic runner."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from numbers import Real
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from edge_model.code_fallback import CodeFallbackRunner
from edge_model.contracts import EdgeResult, PacketInferenceTask


FEATURE_COLUMNS = (
    "vibration.rms",
    "vibration.absolute_peak",
    "vibration.kurtosis",
    "vibration.dominant_frequency_hz",
    "vibration.band_power_ratio_500_2000",
    "vibration.spectral_entropy",
    "phase_current_1.rms_a",
    "phase_current_1.absolute_peak_a",
    "phase_current_2.rms_a",
    "phase_current_2.absolute_peak_a",
    "current_relationship.current_imbalance_ratio",
    "operating_context.shaft_speed_rpm.mean",
    "operating_context.shaft_speed_rpm.last",
    "operating_context.shaft_speed_rpm.minimum",
    "operating_context.shaft_speed_rpm.maximum",
    "operating_context.shaft_speed_rpm.standard_deviation",
    "operating_context.load_torque_nm.mean",
    "operating_context.load_torque_nm.last",
    "operating_context.load_torque_nm.minimum",
    "operating_context.load_torque_nm.maximum",
    "operating_context.load_torque_nm.standard_deviation",
    "operating_context.bearing_radial_load_n.mean",
    "operating_context.bearing_radial_load_n.last",
    "operating_context.bearing_radial_load_n.minimum",
    "operating_context.bearing_radial_load_n.maximum",
    "operating_context.bearing_radial_load_n.standard_deviation",
    "operating_context.bearing_module_temperature_c",
)
LABELS = ("healthy", "outer_ring_damage", "inner_ring_damage")
_LABEL_TO_EDGE = {
    "healthy": ("normal", "low"),
    "outer_ring_damage": ("fault", "high"),
    "inner_ring_damage": ("fault", "high"),
}


class RandomForestDiagnosticModel(CodeFallbackRunner):
    """Load a hash-bound unqualified model and infer one perception packet."""

    def __init__(self, model_path: Path | str, metadata_path: Path | str):
        self.model_path = Path(model_path).resolve()
        self.metadata_path = Path(metadata_path).resolve()
        self.metadata = self._load_metadata()
        self.rule_version = str(self.metadata["model_version"])
        self.artifact = joblib.load(self.model_path)
        self.estimator = self._validate_artifact(self.artifact)
        if hasattr(self.estimator, "n_jobs"):
            self.estimator.n_jobs = 1
        self._last_diagnosis: tuple[str, dict[str, Any]] | None = None
        self._mutex = threading.Lock()

    def run(self, task: PacketInferenceTask) -> EdgeResult:
        self._validate_input(task)
        features = _flatten_features(task.perception)
        frame = pd.DataFrame([[features[name] for name in FEATURE_COLUMNS]], columns=FEATURE_COLUMNS)
        label = str(self.estimator.predict(frame)[0])
        if label not in _LABEL_TO_EDGE:
            raise ValueError("random_forest: unknown predicted label=%s" % label)
        probabilities = self.estimator.predict_proba(frame)[0]
        positions = {str(value): index for index, value in enumerate(self.estimator.classes_)}
        if label not in positions:
            raise ValueError("random_forest: predicted label missing from classes")
        confidence = float(probabilities[positions[label]])
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("random_forest: invalid probability")
        edge_result, risk = _LABEL_TO_EDGE[label]
        output = EdgeResult(
            edge_result=edge_result,
            confidence=confidence,
            edge_risk_level=risk,
            model_version=self.rule_version,
        )
        self._validate_output(output)
        quality = task.perception["perception_quality"]
        warning = quality["status"] == "warning"
        diagnosis = {
            "request_id": task.request_id,
            "diagnosis_label": label,
            "diagnosis_mode": "TEMP_PACKET_MODEL",
            "window_ms": 50,
            "deployment_status": "INTEGRATION_ONLY",
            "feature_columns": list(FEATURE_COLUMNS),
            "review_required": warning,
            "review_reasons": ["PERCEPTION_QUALITY_WARNING"] if warning else [],
        }
        with self._mutex:
            self._last_diagnosis = (task.request_id, diagnosis)
        return output

    def last_diagnosis(self, request_id: str) -> dict[str, Any] | None:
        with self._mutex:
            if self._last_diagnosis is None or self._last_diagnosis[0] != request_id:
                return None
            return dict(self._last_diagnosis[1])

    def _load_metadata(self) -> dict[str, Any]:
        if not self.model_path.is_file():
            raise ValueError("random_forest: model file does not exist")
        if not self.metadata_path.is_file():
            raise ValueError("random_forest: metadata file does not exist")
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        if metadata.get("qualified_for_deployment") is not False:
            raise ValueError("random_forest: artifact must be explicitly integration-only")
        if metadata.get("allowed_use") != "pipeline_integration_only":
            raise ValueError("random_forest: allowed_use is not integration-only")
        if metadata.get("locked_test_consumed") is not False:
            raise ValueError("random_forest: locked test must remain unconsumed")
        if tuple(metadata.get("feature_columns", ())) != FEATURE_COLUMNS:
            raise ValueError("random_forest: metadata feature_columns mismatch")
        if tuple(metadata.get("labels", ())) != LABELS:
            raise ValueError("random_forest: metadata labels mismatch")
        if metadata.get("model_sha256") != _sha256(self.model_path):
            raise ValueError("random_forest: model SHA-256 mismatch")
        version = metadata.get("model_version")
        if not isinstance(version, str) or not version:
            raise ValueError("random_forest: model_version missing")
        return metadata

    @staticmethod
    def _validate_artifact(artifact: Any):
        if not isinstance(artifact, dict):
            raise ValueError("random_forest: artifact must be a mapping")
        if tuple(artifact.get("feature_columns", ())) != FEATURE_COLUMNS:
            raise ValueError("random_forest: artifact feature_columns mismatch")
        if tuple(artifact.get("labels", ())) != LABELS:
            raise ValueError("random_forest: artifact labels mismatch")
        estimator = artifact.get("estimator")
        if estimator is None or not all(
            hasattr(estimator, name) for name in ("predict", "predict_proba", "classes_")
        ):
            raise ValueError("random_forest: invalid estimator")
        learned_columns = getattr(estimator, "feature_names_in_", None)
        if learned_columns is None or tuple(map(str, learned_columns)) != FEATURE_COLUMNS:
            raise ValueError("random_forest: estimator feature_columns mismatch")
        if set(map(str, estimator.classes_)) != set(LABELS):
            raise ValueError("random_forest: estimator classes mismatch")
        return estimator


def _flatten_features(perception: dict[str, Any]) -> dict[str, float]:
    root: Any = perception["features"]
    output: dict[str, float] = {}
    for path in FEATURE_COLUMNS:
        value: Any = root
        try:
            for part in path.split("."):
                value = value[part]
        except (KeyError, TypeError) as exc:
            raise ValueError("random_forest: missing feature %s" % path) from exc
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise ValueError("random_forest: feature must be finite %s" % path)
        output[path] = float(value)
    return output


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
