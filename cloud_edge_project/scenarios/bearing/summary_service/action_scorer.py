"""Pure-function ``action_scorer_v1`` implementation owned by Summary.

No database or network dependencies.  The numeric validation of
``class_probabilities``, the entropy / uncertainty / risk-prior formula, and the
9-field result structure all live here.  Shared thresholds and action mappings
come from :mod:`core.action_level_contract`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from core.action_level_contract import (
    ACTION_LEVEL_TO_ACTION,
    ACTION_SCORER_VERSION,
    action_level_for_score,
)

H5_CLASS_LABELS = (
    "healthy",
    "outer_ring_damage",
    "inner_ring_damage",
)

R_UNCERTAIN_BY_RISK = {
    "low": 0.35,
    "medium": 0.45,
    "high": 0.55,
}

SCORE_SUM_TOLERANCE = 1e-5


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validate_probability(raw_value: Any, label: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValueError(f"class probability {label!r} must be numeric")
    value = float(raw_value)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"class probability {label!r} must be finite and >= 0")
    return value


def score_bearing_action(
    class_probabilities: Mapping[str, float],
    risk_level: str,
    data_quality_score: float,
) -> dict[str, Any]:
    """Score one bearing result into the 9-field ``action_scorer_v1`` result."""

    if set(class_probabilities) != set(H5_CLASS_LABELS):
        raise ValueError(
            "class_probabilities must contain exactly the three H5 labels"
        )

    raw = {
        label: _validate_probability(class_probabilities[label], label)
        for label in H5_CLASS_LABELS
    }

    if (
        isinstance(data_quality_score, bool)
        or not isinstance(data_quality_score, (int, float))
    ):
        raise ValueError("data_quality_score must be numeric")

    quality = float(data_quality_score)
    if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
        raise ValueError("data_quality_score must be finite and in [0, 1]")

    if risk_level not in R_UNCERTAIN_BY_RISK:
        raise ValueError("risk_level must be low, medium, or high")

    probability_sum = sum(raw.values())
    if probability_sum <= 0.0:
        raise ValueError("class probabilities must have at least one positive value")
    if abs(probability_sum - 1.0) > SCORE_SUM_TOLERANCE:
        raise ValueError("class probabilities must sum to 1 within tolerance")

    normalized = {
        label: raw[label] / probability_sum
        for label in H5_CLASS_LABELS
    }

    fault_probability = (
        normalized["outer_ring_damage"] + normalized["inner_ring_damage"]
    )

    entropy_sum = sum(
        probability * math.log(probability)
        for probability in normalized.values()
        if probability > 0.0
    )
    normalized_entropy = _clamp01(-entropy_sum / math.log(3.0))

    final_uncertainty = _clamp01(
        1.0 - (1.0 - normalized_entropy) * quality
    )

    uncertain_risk_prior = R_UNCERTAIN_BY_RISK[risk_level]

    action_score = _clamp01(
        (1.0 - final_uncertainty) * fault_probability
        + final_uncertainty * uncertain_risk_prior
    )

    action_level = action_level_for_score(action_score)

    return {
        "action_scorer_version": ACTION_SCORER_VERSION,
        "normalized_class_probabilities": normalized,
        "fault_probability": fault_probability,
        "normalized_entropy": normalized_entropy,
        "final_uncertainty": final_uncertainty,
        "uncertain_risk_prior": uncertain_risk_prior,
        "action_score": action_score,
        "action_level": action_level,
        "scored_action": ACTION_LEVEL_TO_ACTION[action_level],
    }
