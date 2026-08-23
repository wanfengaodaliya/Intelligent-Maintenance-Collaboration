"""Scenario-neutral candidate detection from computed analysis outputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from cloud_service.global_analysis.runtime_contracts import GlobalAnalysisRuntimeConfig


def detect_problem_candidates(
    *,
    device_health: dict[str, Any],
    packet_diagnosis: dict[str, Any],
    device_arbitration: dict[str, Any],
    previous_analysis: list[dict[str, Any]],
    config: GlobalAnalysisRuntimeConfig,
    scenario_results: Mapping[str, Any] | None = None,
    detect_scenario_candidates: Callable[
        [Mapping[str, Any], list[dict[str, Any]], object],
        list[dict[str, Any]],
    ] | None = None,
    **legacy_scenario_inputs: Any,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if (
        packet_diagnosis.get("status") == "succeeded"
        and packet_diagnosis.get("cloud_correction_rate", 0) >= config.packet_correction_warning_rate
    ):
        under = packet_diagnosis.get("risk_underestimation_rate") or 0
        over = packet_diagnosis.get("risk_overestimation_rate") or 0
        candidates.append(build_problem_candidate(
            "packet_diagnosis", "risk_underestimation" if under >= over else "risk_overestimation",
            "high" if under >= over else "medium",
            {"sample_count": packet_diagnosis.get("reviewed_packet_count"), "cloud_correction_rate": packet_diagnosis.get("cloud_correction_rate"), "risk_underestimation_rate": under},
            "model_update", previous_analysis,
        ))
    if detect_scenario_candidates is not None:
        candidates.extend(
            detect_scenario_candidates(
                scenario_results or {},
                previous_analysis,
                config,
            )
        )
    elif legacy_scenario_inputs:
        from compatibility.bearing_v12 import global_analysis_exports

        candidates.extend(
            global_analysis_exports.detect_legacy_scenario_candidates(
                legacy_scenario_inputs,
                previous_analysis,
                config,
            )
        )
    conflict = device_arbitration.get("conflict_rate")
    if conflict is not None and conflict > config.conflict_rate_target:
        evidence = {
            "conflict_rate": conflict,
            "sample_count": device_arbitration.get("arbitration_count") or 0,
        }
        candidates.append(build_problem_candidate("device_arbitration", "high_conflict_rate", "medium", evidence, "arbitration_rule_review", previous_analysis))
        # 高冲突率常因边端模型对样本判断错误，直接作为模型更新信号。
        candidates.append(build_problem_candidate(
            "device_arbitration", "high_conflict_rate_model", "medium", evidence,
            "model_update", previous_analysis,
        ))
    success = device_arbitration.get("arbitration_success_rate")
    if success is not None and success < config.arbitration_success_target:
        candidates.append(build_problem_candidate("device_arbitration", "low_arbitration_success_rate", "high", {"arbitration_success_rate": success}, "arbitration_rule_review", previous_analysis))
    return candidates


def build_problem_candidate(layer: str, kind: str, severity: str, evidence: dict[str, Any], action: str, previous: list[dict[str, Any]]) -> dict[str, Any]:
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
