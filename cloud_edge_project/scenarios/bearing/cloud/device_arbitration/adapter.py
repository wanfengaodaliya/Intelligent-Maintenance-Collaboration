from __future__ import annotations

import math
from typing import Any

from core.arbitration_contracts import (
    ArbitrationContext,
    ArbitrationValidationError,
    DecisionUnit,
    RuleDecision,
)
from scenarios.bearing.cloud.device_arbitration.config import (
    ACTION_SEVERITY,
    ACTION_TO_STATE,
    DEFAULT_CONFIG,
)


_STATES = {"normal", "warning", "fault", "unknown"}
_RISK_LEVELS = {"low", "medium", "high"}


class BearingDeviceArbitrationAdapter:
    scenario_type = "bearing"

    def build_context(self, request: dict[str, Any]) -> ArbitrationContext:
        if not isinstance(request, dict):
            raise ArbitrationValidationError("INVALID_REQUEST", "request must be an object")
        conflict_id = _identifier(request, "conflict_id")
        subject_id = _identifier(request, "subject_id")
        task_id = _identifier(request, "task_id")
        payload = request.get("scenario_payload")
        if not isinstance(payload, dict):
            raise ArbitrationValidationError(
                "INVALID_REQUEST", "scenario_payload must be an object"
            )
        results = payload.get("bearing_results")
        if not isinstance(results, list) or len(results) < 2:
            raise ArbitrationValidationError(
                "INVALID_REQUEST", "bearing_results must contain at least two results"
            )

        units: list[DecisionUnit] = []
        bearing_ids: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                raise ArbitrationValidationError(
                    "INVALID_REQUEST", "each bearing result must be an object"
                )
            bearing_id = _identifier(item, "bearing_id")
            if bearing_id in bearing_ids:
                raise ArbitrationValidationError(
                    "DUPLICATE_BEARING", "bearing_id values must be unique"
                )
            bearing_ids.add(bearing_id)
            state = item.get("bearing_state")
            if state not in _STATES:
                raise ArbitrationValidationError(
                    "INVALID_REQUEST", "bearing_state is not supported"
                )
            risk_level = item.get("risk_level")
            if risk_level not in _RISK_LEVELS:
                raise ArbitrationValidationError(
                    "INVALID_RISK_LEVEL", "risk_level is not supported"
                )
            action = item.get("recommended_action")
            if action not in ACTION_SEVERITY:
                raise ArbitrationValidationError(
                    "INVALID_ACTION", "recommended_action is not supported"
                )
            units.append(
                DecisionUnit(
                    unit_id=bearing_id,
                    state=state,
                    confidence=_score(item.get("confidence"), "INVALID_CONFIDENCE"),
                    data_quality_score=_score(
                        item.get("data_quality_score"), "INVALID_DATA_QUALITY"
                    ),
                    risk_level=risk_level,
                    recommended_action=action,
                )
            )
        return ArbitrationContext(
            scenario_type=self.scenario_type,
            conflict_id=conflict_id,
            subject_id=subject_id,
            task_id=task_id,
            decision_units=units,
        )

    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision:
        from scenarios.bearing.cloud.device_arbitration.rules import evaluate_rules

        return evaluate_rules(context, DEFAULT_CONFIG)

    def action_to_state(self, action: str) -> str:
        return ACTION_TO_STATE[action]

    def action_severity(self) -> dict[str, int]:
        return dict(ACTION_SEVERITY)

    def decision_thresholds(self) -> tuple[float, float]:
        return DEFAULT_CONFIG.min_top_score, DEFAULT_CONFIG.min_margin

    def build_scenario_result(
        self,
        *,
        context: ArbitrationContext,
        dominant_unit_id: str | None,
        triggered_rule_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "device_id": context.subject_id,
            "dominant_bearing_id": dominant_unit_id,
            "triggered_rule_id": triggered_rule_id,
            "reason": reason,
            "rule_version": DEFAULT_CONFIG.rule_version,
        }


def _identifier(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ArbitrationValidationError("INVALID_REQUEST", f"{field} is required")
    return value.strip()


def _score(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArbitrationValidationError(code, "score must be a finite number in [0, 1]")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ArbitrationValidationError(code, "score must be in [0, 1]")
    return score
