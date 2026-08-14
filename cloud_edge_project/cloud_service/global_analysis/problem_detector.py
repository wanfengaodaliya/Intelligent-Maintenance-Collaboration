"""Rule-based candidate detection from already-computed analysis outputs."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cloud_service.global_analysis.contracts import GlobalAnalysisConfig


def detect_problem_candidates(
    *,
    device_health: dict[str, Any],
    bearing_risk: dict[str, Any],
    packet_diagnosis: dict[str, Any],
    cloud_bearing_review: dict[str, Any],
    device_arbitration: dict[str, Any],
    previous_analysis: list[dict[str, Any]],
    config: GlobalAnalysisConfig,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if (
        packet_diagnosis.get("status") == "succeeded"
        and packet_diagnosis.get("cloud_correction_rate", 0) >= config.packet_correction_warning_rate
    ):
        under = packet_diagnosis.get("risk_underestimation_rate") or 0
        over = packet_diagnosis.get("risk_overestimation_rate") or 0
        candidates.append(_candidate(
            "packet_diagnosis", "risk_underestimation" if under >= over else "risk_overestimation",
            "high" if under >= over else "medium",
            {"sample_count": packet_diagnosis.get("reviewed_packet_count"), "cloud_correction_rate": packet_diagnosis.get("cloud_correction_rate"), "risk_underestimation_rate": under},
            "model_update", previous_analysis,
        ))
    if (
        cloud_bearing_review.get("status") == "succeeded"
        and cloud_bearing_review.get("bearing_correction_rate", 0) >= config.bearing_correction_warning_rate
    ):
        candidates.append(_candidate(
            "cloud_bearing_review", "high_correction_rate", "medium",
            {"sample_count": cloud_bearing_review.get("bearing_review_count"), "bearing_correction_rate": cloud_bearing_review.get("bearing_correction_rate")},
            "cloud_review_policy_review", previous_analysis,
        ))
    conflict = device_arbitration.get("conflict_rate")
    if conflict is not None and conflict > config.conflict_rate_target:
        candidates.append(_candidate("device_arbitration", "high_conflict_rate", "medium", {"conflict_rate": conflict}, "arbitration_rule_review", previous_analysis))
    success = device_arbitration.get("arbitration_success_rate")
    if success is not None and success < config.arbitration_success_target:
        candidates.append(_candidate("device_arbitration", "low_arbitration_success_rate", "high", {"arbitration_success_rate": success}, "arbitration_rule_review", previous_analysis))
    return candidates


def _candidate(layer: str, kind: str, severity: str, evidence: dict[str, Any], action: str, previous: list[dict[str, Any]]) -> dict[str, Any]:
    occurrences = sum(
        1 for result in previous[-3:]
        for candidate in result.get("problem_candidates", [])
        if candidate.get("problem_layer") == layer and candidate.get("problem_type") == kind
    )
    persistence = "unknown" if not previous else "persistent" if occurrences >= 2 else "temporary"
    return {
        "problem_id": f"problem_{uuid4().hex}", "problem_layer": layer, "problem_type": kind,
        "severity": severity, "problem_context": {}, "evidence": evidence,
        "persistence": persistence, "suggested_action": action,
    }
