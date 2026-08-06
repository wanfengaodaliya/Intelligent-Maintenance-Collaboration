from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Mapping

from core.arbitration_contracts import DecisionUnit


def calculate_fusion(
    units: Iterable[DecisionUnit],
    *,
    action_severity: Mapping[str, int],
    min_top_score: float,
    min_margin: float,
) -> dict[str, object]:
    unit_list = list(units)
    raw_scores: dict[str, float] = defaultdict(float)
    weights: dict[str, float] = {}
    for unit in unit_list:
        weight = unit.confidence * unit.data_quality_score
        raw_scores[unit.recommended_action] += weight
        weights[unit.unit_id] = weight

    total_score = sum(raw_scores.values())
    if total_score <= 0.0:
        return {
            "status": "manual_review",
            "final_action": None,
            "confidence": 0.0,
            "dominant_unit_id": None,
            "action_scores": dict(raw_scores),
            "decision_margin": 0.0,
            "reason": "no positive decision weight is available",
        }

    action_scores = {
        action: score / total_score for action, score in raw_scores.items()
    }
    ranked = sorted(
        action_scores.items(),
        key=lambda item: (item[1], action_severity.get(item[0], -1)),
        reverse=True,
    )
    top_action, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top_score - second_score
    tie = len(ranked) > 1 and math.isclose(
        top_score, second_score, rel_tol=0.0, abs_tol=1e-12
    )
    resolved = top_score >= min_top_score and (tie or margin >= min_margin)
    if not resolved:
        return {
            "status": "manual_review",
            "final_action": None,
            "confidence": top_score,
            "dominant_unit_id": None,
            "action_scores": action_scores,
            "decision_margin": margin,
            "reason": "weighted action scores do not meet the decision threshold",
        }

    candidates = [
        unit
        for unit in unit_list
        if unit.recommended_action == top_action
    ]
    dominant = max(candidates, key=lambda unit: (weights[unit.unit_id], unit.unit_id))
    return {
        "status": "resolved",
        "final_action": top_action,
        "confidence": top_score,
        "dominant_unit_id": dominant.unit_id,
        "action_scores": action_scores,
        "decision_margin": margin,
        "reason": "weighted action fusion selected the highest supported action",
    }
