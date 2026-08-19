"""Pluggable cloud diagnosis-model adapter with deterministic fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DiagnosisFeatures:
    numeric_features: dict[str, float]
    operating_conditions: dict[str, float | None]
    quality_codes: list[str]
    bearing_match_scores: dict[str, float]


class DiagnosisModel(Protocol):
    model_version: str

    def predict(self, features: DiagnosisFeatures) -> dict[str, Any]:
        ...


class RuleBasedDiagnosisAdapter:
    model_version = "cloud-diagnosis-v1"

    def predict(self, features: DiagnosisFeatures) -> dict[str, Any]:
        scores = dict(features.bearing_match_scores)
        max_score = max(scores.values(), default=0.0)
        fault = max_score >= 0.60
        if fault:
            probability = min(0.99, max(0.50, max_score))
        else:
            probability = min(0.99, max(0.50, 1.0 - max_score))
        label = "fault" if fault else "normal"
        probability = round(float(probability), 4)
        return {
            "status": "available",
            "label": label,
            "label_probabilities": {
                "normal": round(1.0 - probability, 4),
                "fault": round(probability, 4),
            },
            "probability": probability,
            "uncertainty": round(1.0 - probability, 4),
            "model_version": self.model_version,
            "feature_contributions": [
                {"name": name, "value": round(float(value), 4)}
                for name, value in sorted(scores.items())
            ],
        }
