from __future__ import annotations

from core.arbitration_contracts import ArbitrationContext, RuleDecision
from core.arbitration_engine import ArbitrationEngine


class _Policy:
    scenario_type = "inspection"

    def __init__(self, rule: RuleDecision) -> None:
        self.rule = rule

    def evaluate_rules(self, context: ArbitrationContext) -> RuleDecision:
        return self.rule

    def action_severity(self) -> dict[str, int]:
        return {"continue": 0, "stop": 1}

    def decision_thresholds(self) -> tuple[float, float]:
        return 0.6, 0.1


def _context() -> ArbitrationContext:
    return ArbitrationContext(
        scenario_type="inspection",
        conflict_id="conflict-1",
        subject_id="device-1",
        task_id="task-1",
        decision_units=[],
    )


def test_arbitration_engine_uses_scenario_rule_before_fusion() -> None:
    policy = _Policy(
        RuleDecision(
            triggered=True,
            rule_id="safe-stop",
            final_action="stop",
            confidence=0.9,
            dominant_unit_id="unit-a",
            reason="safety rule",
        )
    )

    def unexpected_fusion(*args, **kwargs):
        raise AssertionError("fusion must not run after a scenario rule triggers")

    decision = ArbitrationEngine(policy, unexpected_fusion).decide(_context())

    assert decision["resolution_method"] == "scenario_rule"
    assert decision["final_action"] == "stop"
    assert decision["triggered_rule_id"] == "safe-stop"


def test_arbitration_engine_falls_back_to_injected_weighted_fusion() -> None:
    policy = _Policy(RuleDecision(triggered=False))
    received: dict[str, object] = {}

    def fusion(units, *, action_severity, min_top_score, min_margin):
        received.update(
            units=list(units),
            action_severity=action_severity,
            min_top_score=min_top_score,
            min_margin=min_margin,
        )
        return {
            "status": "manual_review",
            "final_action": None,
            "confidence": 0.0,
            "dominant_unit_id": None,
            "action_scores": {},
            "decision_margin": 0.0,
            "reason": "no decision",
        }

    decision = ArbitrationEngine(policy, fusion).decide(_context())

    assert decision["resolution_method"] == "weighted_fusion"
    assert decision["triggered_rule_id"] is None
    assert received == {
        "units": [],
        "action_severity": {"continue": 0, "stop": 1},
        "min_top_score": 0.6,
        "min_margin": 0.1,
    }
