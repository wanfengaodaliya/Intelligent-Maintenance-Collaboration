from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ArbitrationValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DecisionUnit:
    unit_id: str
    state: str
    confidence: float
    data_quality_score: float
    risk_level: str
    recommended_action: str
    scenario_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArbitrationContext:
    scenario_type: str
    conflict_id: str
    subject_id: str
    task_id: str
    decision_units: list[DecisionUnit]


@dataclass(frozen=True)
class RuleDecision:
    triggered: bool
    rule_id: str | None = None
    final_action: str | None = None
    confidence: float | None = None
    dominant_unit_id: str | None = None
    reason: str | None = None


class ScenarioArbitrationAdapter(Protocol):
    scenario_type: str

    def build_context(self, request: dict[str, Any]) -> ArbitrationContext: ...

    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision: ...

    def action_to_state(self, action: str) -> str: ...

    def action_severity(self) -> dict[str, int]: ...

    def decision_thresholds(self) -> tuple[float, float]: ...

    def build_scenario_result(
        self,
        *,
        context: ArbitrationContext,
        dominant_unit_id: str | None,
        triggered_rule_id: str | None,
        reason: str,
    ) -> dict[str, Any]: ...
