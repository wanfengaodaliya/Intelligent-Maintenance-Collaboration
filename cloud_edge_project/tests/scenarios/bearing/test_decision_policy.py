from __future__ import annotations

import pytest

from bootstrap.scenarios import build_scenario_registry
from core.scenario_contracts import ScenarioDecision, ScenarioDiagnosis
from core.scenario_plugin import DECISION_POLICY


def _diagnosis(**overrides: object) -> ScenarioDiagnosis:
    values: dict[str, object] = {
        "scenario_id": "bearing",
        "task_id": "task_001",
        "unit_id": "bearing_01",
        "state": "warning",
        "confidence": 0.87,
        "risk_level": "medium",
        "action_level": 2,
        "model_id": "distilled_h5",
        "model_version": "v1",
        "evidence": {"source": "edge"},
    }
    values.update(overrides)
    return ScenarioDiagnosis(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("action_level", "expected_action"),
    (
        (0, "continue_operation"),
        (1, "enhanced_monitoring"),
        (2, "scheduled_inspection"),
        (3, "urgent_intervention"),
        (4, "shutdown"),
    ),
)
def test_bearing_decision_policy_preserves_diagnosis_and_action_mapping(
    action_level: int,
    expected_action: str,
) -> None:
    policy = build_scenario_registry().require_provider(
        "bearing", DECISION_POLICY
    )

    decision = policy.decide(_diagnosis(action_level=action_level))

    assert decision == ScenarioDecision(
        scenario_id="bearing",
        task_id="task_001",
        unit_id="bearing_01",
        state="warning",
        confidence=0.87,
        risk_level="medium",
        action_level=action_level,
        decision=expected_action,
        evidence={"source": "edge"},
    )


def test_bearing_decision_policy_rejects_other_scenarios() -> None:
    policy = build_scenario_registry().require_provider(
        "bearing", DECISION_POLICY
    )

    with pytest.raises(ValueError, match="scenario_id must be bearing"):
        policy.decide(_diagnosis(scenario_id="inspection"))
