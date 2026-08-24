"""Bidirectional adapters between bearing V1.2 and scenario-neutral results."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TypeVar

from compatibility.bearing_v12.diagnosis_contracts import (
    BearingDecisionResult,
    CloudBearingResult,
    DeviceDecisionResult,
    EdgeBearingResult,
)
from core.scenario_contracts import ScenarioDecision, ScenarioDiagnosis


LegacyDiagnosis = TypeVar("LegacyDiagnosis", EdgeBearingResult, CloudBearingResult)


def edge_bearing_to_scenario(result: EdgeBearingResult) -> ScenarioDiagnosis:
    return _diagnosis_to_scenario(result, model_id="distilled_h5")


def cloud_bearing_to_scenario(result: CloudBearingResult) -> ScenarioDiagnosis:
    return _diagnosis_to_scenario(result, model_id="moment_light_adapt")


def scenario_to_edge_bearing(
    diagnosis: ScenarioDiagnosis,
    template: EdgeBearingResult,
) -> EdgeBearingResult:
    _require_template(template, EdgeBearingResult)
    return _scenario_to_diagnosis(diagnosis, template)


def scenario_to_cloud_bearing(
    diagnosis: ScenarioDiagnosis,
    template: CloudBearingResult,
) -> CloudBearingResult:
    _require_template(template, CloudBearingResult)
    return _scenario_to_diagnosis(diagnosis, template)


def bearing_decision_to_scenario(result: BearingDecisionResult) -> ScenarioDecision:
    return ScenarioDecision(
        scenario_id="bearing",
        task_id=result.task_id,
        unit_id=result.bearing_id,
        state=result.bearing_state,
        confidence=result.confidence,
        risk_level=result.risk_level,
        action_level=result.action_grade,
        decision=result.recommended_action,
        evidence=_legacy_evidence(
            result,
            exclude={
                "task_id",
                "bearing_id",
                "bearing_state",
                "confidence",
                "risk_level",
                "action_grade",
                "recommended_action",
            },
        ),
    )


def scenario_to_bearing_decision(
    decision: ScenarioDecision,
    template: BearingDecisionResult,
) -> BearingDecisionResult:
    _require_template(template, BearingDecisionResult)
    _require_bearing_scenario(decision.scenario_id)
    return replace(
        template,
        bearing_state=decision.state,
        confidence=decision.confidence,
        risk_level=decision.risk_level,
        action_grade=decision.action_level,
        recommended_action=decision.decision,
    )


def device_decision_to_scenario(result: DeviceDecisionResult) -> ScenarioDecision:
    return ScenarioDecision(
        scenario_id="bearing",
        task_id=result.task_id,
        unit_id=result.device_id,
        state=result.final_state,
        confidence=result.confidence,
        risk_level="unknown",
        action_level=result.final_action_grade,
        decision=result.final_action,
        evidence=_legacy_evidence(
            result,
            exclude={
                "task_id",
                "device_id",
                "final_state",
                "confidence",
                "final_action_grade",
                "final_action",
            },
        ),
    )


def scenario_to_device_decision(
    decision: ScenarioDecision,
    template: DeviceDecisionResult,
) -> DeviceDecisionResult:
    _require_template(template, DeviceDecisionResult)
    _require_bearing_scenario(decision.scenario_id)
    return replace(
        template,
        final_state=decision.state,
        confidence=decision.confidence,
        final_action_grade=decision.action_level,
        final_action=decision.decision,
    )


def _diagnosis_to_scenario(
    result: EdgeBearingResult | CloudBearingResult,
    *,
    model_id: str,
) -> ScenarioDiagnosis:
    return ScenarioDiagnosis(
        scenario_id="bearing",
        task_id=result.task_id,
        unit_id=result.bearing_id,
        state=result.bearing_state,
        confidence=result.confidence,
        risk_level=result.risk_level,
        action_level=result.action_grade,
        model_id=model_id,
        model_version=result.model_version,
        evidence={
            **_legacy_evidence(
                result,
                exclude={
                    "task_id",
                    "bearing_id",
                    "bearing_state",
                    "confidence",
                    "risk_level",
                    "action_grade",
                    "recommended_action",
                    "model_version",
                },
            ),
            "recommended_action": result.recommended_action,
        },
    )


def _scenario_to_diagnosis(
    diagnosis: ScenarioDiagnosis,
    template: LegacyDiagnosis,
) -> LegacyDiagnosis:
    _require_bearing_scenario(diagnosis.scenario_id)
    recommended_action = diagnosis.evidence.get(
        "recommended_action", template.recommended_action
    )
    if not isinstance(recommended_action, str) or not recommended_action:
        recommended_action = template.recommended_action
    return replace(
        template,
        bearing_state=diagnosis.state,
        confidence=diagnosis.confidence,
        risk_level=diagnosis.risk_level,
        action_grade=diagnosis.action_level,
        recommended_action=recommended_action,
        model_version=diagnosis.model_version,
    )


def _legacy_evidence(result: object, *, exclude: set[str]) -> dict[str, object]:
    return {
        field.name: getattr(result, field.name)
        for field in fields(result)
        if field.name not in exclude
    }


def _require_template(template: object, expected_type: type[object]) -> None:
    if not isinstance(template, expected_type):
        raise TypeError(f"template must be {expected_type.__name__}")


def _require_bearing_scenario(scenario_id: str) -> None:
    if scenario_id != "bearing":
        raise ValueError("scenario_id must be bearing")


__all__ = [
    "bearing_decision_to_scenario",
    "cloud_bearing_to_scenario",
    "device_decision_to_scenario",
    "edge_bearing_to_scenario",
    "scenario_to_bearing_decision",
    "scenario_to_cloud_bearing",
    "scenario_to_device_decision",
    "scenario_to_edge_bearing",
]
