from __future__ import annotations

from core.arbitration_contracts import ArbitrationContext, RuleDecision
from scenarios.bearing.cloud.device_arbitration.config import (
    BearingArbitrationConfig,
)


def evaluate_rules(
    context: ArbitrationContext,
    config: BearingArbitrationConfig,
) -> RuleDecision:
    for unit in context.decision_units:
        if (
            unit.state == "fault"
            and unit.risk_level == "high"
            and unit.confidence >= config.abnormal_min_confidence
        ):
            return _shutdown(
                "HIGH_RISK_ABNORMAL",
                unit.unit_id,
                unit.confidence,
                f"{unit.unit_id} is fault with high risk",
            )

    high_risk_units = [
        unit
        for unit in context.decision_units
        if (
            unit.risk_level == "high"
            and unit.confidence >= config.multiple_high_risk_min_confidence
        )
    ]
    if len(high_risk_units) >= config.multiple_high_risk_min_count:
        dominant = max(
            high_risk_units, key=lambda unit: (unit.confidence, unit.unit_id)
        )
        return _shutdown(
            "MULTIPLE_HIGH_RISK",
            dominant.unit_id,
            dominant.confidence,
            "multiple units have high risk",
        )

    return RuleDecision(triggered=False)


def _shutdown(
    rule_id: str,
    dominant_unit_id: str,
    confidence: float,
    reason: str,
) -> RuleDecision:
    return RuleDecision(
        triggered=True,
        rule_id=rule_id,
        final_action="shutdown",
        confidence=confidence,
        dominant_unit_id=dominant_unit_id,
        reason=reason,
    )
