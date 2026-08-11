"""Deterministic placeholder for the future trained bearing diagnostic model."""

from __future__ import annotations

from typing import Any, Mapping

from edge_model.code_fallback import CodeFallbackRunner, _classify_test_rule
from edge_model.contracts import EdgeResult, PacketInferenceTask


class MockDiagnosticModel(CodeFallbackRunner):
    """Classify documented perception features without randomness or an LLM."""

    def __init__(self, model_version: str = "bearing_diagnosis_mock_v1"):
        self.rule_version = model_version

    def run(self, task: PacketInferenceTask) -> EdgeResult:
        self._validate_input(task)
        features = task.perception.get("features") or {}
        flags = (task.perception.get("perception_quality") or {}).get("flags") or []
        result, risk = _classify_test_rule(features, flags)
        confidence = {"normal": 0.88, "warning": 0.78, "fault": 0.90}[result]
        quality = quality_score_from_perception(task.perception)
        confidence = round(max(0.0, min(1.0, confidence * (0.8 + 0.2 * quality))), 3)
        output = EdgeResult(
            edge_result=result,
            confidence=confidence,
            edge_risk_level=risk,
            model_version=self.rule_version,
        )
        self._validate_output(output)
        return output


def quality_score_from_perception(perception: Mapping[str, Any]) -> float:
    """Map the current perception flags to a stable temporary quality score."""

    quality = perception.get("perception_quality")
    if not isinstance(quality, Mapping):
        return 0.0
    flags = quality.get("flags")
    if quality.get("status") == "good" and flags == []:
        return 1.0
    if not isinstance(flags, list):
        return 0.0
    penalty = sum(0.2 if flag == "DEVICE_NOT_RUNNING" else 0.1 for flag in flags)
    return round(max(0.0, 1.0 - penalty), 3)
