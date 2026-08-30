"""Scenario-neutral rule-first arbitration orchestration."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from core.arbitration_contracts import ArbitrationContext, DecisionUnit, RuleDecision


class ArbitrationPolicy(Protocol):
    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision: ...

    def action_severity(self) -> dict[str, int]: ...

    def decision_thresholds(self) -> tuple[float, float]: ...


class FusionCalculator(Protocol):
    def __call__(
        self,
        units: Iterable[DecisionUnit],
        *,
        action_severity: Mapping[str, int],
        min_top_score: float,
        min_margin: float,
    ) -> dict[str, object]: ...


class ArbitrationEngine:
    def __init__(
        self,
        policy: ArbitrationPolicy,
        fusion_calculator: FusionCalculator,
    ) -> None:
        self._policy = policy
        self._fusion_calculator = fusion_calculator

    def decide(self, context: ArbitrationContext) -> dict[str, Any]:
        rule = self._policy.evaluate_rules(context)
        if rule.triggered:
            return {
                "status": "resolved",
                "final_action": rule.final_action,
                "confidence": rule.confidence,
                "dominant_unit_id": rule.dominant_unit_id,
                "action_scores": {rule.final_action: 1.0},
                "resolution_method": "scenario_rule",
                "reason": rule.reason or "scenario safety rule triggered",
                "triggered_rule_id": rule.rule_id,
            }

        min_top_score, min_margin = self._policy.decision_thresholds()
        return self._fusion_calculator(
            context.decision_units,
            action_severity=self._policy.action_severity(),
            min_top_score=min_top_score,
            min_margin=min_margin,
        ) | {
            "resolution_method": "weighted_fusion",
            "triggered_rule_id": None,
        }
