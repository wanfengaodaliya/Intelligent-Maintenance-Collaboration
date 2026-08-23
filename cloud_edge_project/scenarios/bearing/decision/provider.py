"""Bearing-specific policy for combining unit decision revisions."""

from __future__ import annotations

from core.bearing_actions import ACTION_TO_STATE, action_for_grade
from core.consistency_engine import ConsistencyDecision, ConsistencyRequest


class BearingConsistencyPolicy:
    scenario_id = "bearing"

    def evaluate(self, request: ConsistencyRequest) -> ConsistencyDecision:
        expected = request.expected_unit_ids
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        by_unit = {item.unit_id: item for item in request.units}
        if len(by_unit) != len(request.units) or any(item not in expected for item in by_unit):
            raise ValueError("bearing results must be unique expected bearings")
        ordered = tuple(by_unit[item] for item in expected if item in by_unit)
        missing = tuple(item for item in expected if item not in by_unit)

        if request.closure_reason == "ROUND_TIMEOUT":
            status = "INCOMPLETE"
        elif missing:
            raise ValueError("only ROUND_TIMEOUT may close an incomplete round")
        elif any(item.lifecycle_status == "PROVISIONAL" for item in ordered):
            status = "PROVISIONAL"
        else:
            status = "FINAL"

        if not ordered:
            final_grade, confidence, quality = 0, 0.0, 0.0
        else:
            final_grade = max(item.action_level for item in ordered)
            confidence = min(item.confidence for item in ordered)
            quality = min(item.data_quality_score for item in ordered)
        grades = [item.action_level for item in ordered]
        conflict = len(grades) > 1 and max(grades) - min(grades) >= 2
        final_action = action_for_grade(final_grade)
        return ConsistencyDecision(
            status=status,
            received_unit_ids=tuple(item.unit_id for item in ordered),
            missing_unit_ids=missing,
            final_state=ACTION_TO_STATE[final_action],
            final_action_level=final_grade,
            final_action=final_action,
            confidence=confidence,
            data_quality_score=quality,
            has_conflict=conflict,
            conflict_reasons=("DEVICE_ACTION_GRADE_CONFLICT",) if conflict else (),
            degraded=status != "FINAL",
        )

