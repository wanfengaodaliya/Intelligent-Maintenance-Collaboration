from __future__ import annotations

import math

import pytest

from core.action_level_contract import (
    ACTION_LEVEL_TO_ACTION,
    action_level_for_score,
)
from summary_service.action_scorer import (
    H5_CLASS_LABELS,
    R_UNCERTAIN_BY_RISK,
    score_bearing_action,
)


EXPECTED_FIELDS = {
    "action_scorer_version",
    "normalized_class_probabilities",
    "fault_probability",
    "normalized_entropy",
    "final_uncertainty",
    "uncertain_risk_prior",
    "action_score",
    "action_level",
    "scored_action",
}


def _score(probabilities, risk_level="low", data_quality_score=1.0):
    return score_bearing_action(
        class_probabilities=probabilities,
        risk_level=risk_level,
        data_quality_score=data_quality_score,
    )


def _probs(healthy, outer, inner):
    return {
        "healthy": healthy,
        "outer_ring_damage": outer,
        "inner_ring_damage": inner,
    }


def test_returns_exactly_nine_fields() -> None:
    result = _score(_probs(1.0, 0.0, 0.0))

    assert set(result) == EXPECTED_FIELDS
    assert result["action_scorer_version"] == "action_scorer_v1"


def test_fully_confident_healthy() -> None:
    result = _score(_probs(1.0, 0.0, 0.0), risk_level="low", data_quality_score=1.0)

    assert result["fault_probability"] == pytest.approx(0.0)
    assert result["normalized_entropy"] == pytest.approx(0.0)
    assert result["final_uncertainty"] == pytest.approx(0.0)
    assert result["action_score"] == pytest.approx(0.0)
    assert result["action_level"] == 0
    assert result["scored_action"] == "continue_operation"


def test_fully_confident_fault() -> None:
    result = _score(_probs(0.0, 1.0, 0.0), risk_level="high", data_quality_score=1.0)

    assert result["fault_probability"] == pytest.approx(1.0)
    assert result["normalized_entropy"] == pytest.approx(0.0)
    assert result["final_uncertainty"] == pytest.approx(0.0)
    assert result["action_score"] == pytest.approx(1.0)
    assert result["action_level"] == 3
    assert result["scored_action"] == "shutdown"


def test_uniform_low_risk() -> None:
    result = _score(_probs(1 / 3, 1 / 3, 1 / 3), risk_level="low", data_quality_score=1.0)

    assert result["normalized_entropy"] == pytest.approx(1.0)
    assert result["final_uncertainty"] == pytest.approx(1.0)
    assert result["action_score"] == pytest.approx(0.35)
    assert result["action_level"] == 1
    assert result["scored_action"] == "enhanced_monitoring"


def test_uniform_high_risk() -> None:
    result = _score(_probs(1 / 3, 1 / 3, 1 / 3), risk_level="high", data_quality_score=1.0)

    assert result["action_score"] == pytest.approx(0.55)
    assert result["action_level"] == 2
    assert result["scored_action"] == "scheduled_inspection"


@pytest.mark.parametrize(
    ("risk_level", "expected_score", "expected_level"),
    [
        ("low", 0.35, 1),
        ("medium", 0.45, 2),
        ("high", 0.55, 2),
    ],
)
def test_zero_data_quality_uses_risk_prior(
    risk_level: str, expected_score: float, expected_level: int
) -> None:
    result = _score(_probs(0.8, 0.1, 0.1), risk_level=risk_level, data_quality_score=0.0)

    assert result["final_uncertainty"] == pytest.approx(1.0)
    assert result["action_score"] == pytest.approx(expected_score)
    assert result["action_level"] == expected_level


def test_risk_prior_constants() -> None:
    assert R_UNCERTAIN_BY_RISK == {"low": 0.35, "medium": 0.45, "high": 0.55}


def test_probabilities_are_renormalized_without_mutating_input() -> None:
    raw = _probs(0.999999, 0.0, 0.0)
    original = dict(raw)

    result = _score(raw, risk_level="low", data_quality_score=1.0)

    assert raw == original  # untouched
    assert result["normalized_class_probabilities"]["healthy"] == pytest.approx(1.0)
    assert result["fault_probability"] == pytest.approx(0.0)


def test_normalized_probabilities_sum_to_one() -> None:
    result = _score(_probs(0.7, 0.2, 0.1))

    assert sum(result["normalized_class_probabilities"].values()) == pytest.approx(1.0)
    assert list(result["normalized_class_probabilities"]) == list(H5_CLASS_LABELS)


def test_entropy_skips_zero_probabilities() -> None:
    # One-hot vector must not raise on log(0).
    result = _score(_probs(0.0, 1.0, 0.0))

    assert result["normalized_entropy"] == pytest.approx(0.0)


def test_action_score_matches_composition_formula() -> None:
    for probabilities in (
        _probs(0.8, 0.1, 0.1),
        _probs(0.5, 0.3, 0.2),
        _probs(0.1, 0.6, 0.3),
    ):
        for risk_level in ("low", "medium", "high"):
            result = _score(
                dict(probabilities),
                risk_level=risk_level,
                data_quality_score=0.6,
            )
            expected = (
                (1.0 - result["final_uncertainty"]) * result["fault_probability"]
                + result["final_uncertainty"] * result["uncertain_risk_prior"]
            )
            assert result["action_score"] == pytest.approx(expected)


def test_scored_fields_follow_level_mapping() -> None:
    for level in range(4):
        assert ACTION_LEVEL_TO_ACTION[level] in {
            "continue_operation",
            "enhanced_monitoring",
            "scheduled_inspection",
            "shutdown",
        }

    result = _score(_probs(0.0, 0.5, 0.5), risk_level="high", data_quality_score=1.0)
    assert result["scored_action"] == ACTION_LEVEL_TO_ACTION[result["action_level"]]


# ---------------------------------------------------------------------------
# Threshold boundaries (shared action_level_for_score)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0.199999, 0),
        (0.200000, 1),
        (0.449999, 1),
        (0.450000, 2),
        (0.749999, 2),
        (0.750000, 3),
    ],
)
def test_action_level_thresholds(score: float, level: int) -> None:
    assert action_level_for_score(score) == level


def test_acceptance_example_levels() -> None:
    # Plan appendix B.1: 0.32 -> level 1, 0.48 -> level 2.
    assert action_level_for_score(0.32) == 1
    assert action_level_for_score(0.48) == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probabilities",
    [
        {"healthy": 0.8, "outer_ring_damage": 0.1},  # missing label
        {"healthy": 0.8, "outer_ring_damage": 0.1, "inner_ring_damage": 0.1, "extra": 0.0},  # extra label
        {"healthy": -0.1, "outer_ring_damage": 0.5, "inner_ring_damage": 0.6},  # negative
        {"healthy": 0.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0},  # all zero
        {"healthy": 0.5, "outer_ring_damage": 0.5, "inner_ring_damage": 0.5},  # sum 1.5
        {"healthy": 0.8, "outer_ring_damage": 0.1, "inner_ring_damage": 0.1001},  # sum 1.0001
        {"healthy": float("nan"), "outer_ring_damage": 0.5, "inner_ring_damage": 0.5},  # NaN
        {"healthy": float("inf"), "outer_ring_damage": 0.0, "inner_ring_damage": 0.0},  # Infinity
        {"healthy": True, "outer_ring_damage": 0.5, "inner_ring_damage": 0.5},  # bool
        {"healthy": "0.8", "outer_ring_damage": 0.1, "inner_ring_damage": 0.1},  # non-numeric
    ],
)
def test_rejects_invalid_probabilities(probabilities) -> None:
    with pytest.raises(ValueError):
        _score(probabilities)


def test_rejects_invalid_risk_level() -> None:
    with pytest.raises(ValueError, match="risk_level"):
        _score(_probs(0.8, 0.1, 0.1), risk_level="extreme")


@pytest.mark.parametrize("quality", [-0.1, 1.1, float("nan"), float("inf"), True, "0.8"])
def test_rejects_invalid_data_quality(quality) -> None:
    with pytest.raises(ValueError):
        _score(_probs(0.8, 0.1, 0.1), data_quality_score=quality)


def test_output_always_within_unit_interval() -> None:
    cases = [
        (_probs(0.0, 0.5, 0.5), "low", 1.0),
        (_probs(0.5, 0.5, 0.0), "medium", 0.5),
        (_probs(0.9, 0.05, 0.05), "high", 0.0),
        (_probs(0.2, 0.4, 0.4), "high", 1.0),
    ]
    for probabilities, risk_level, quality in cases:
        result = _score(probabilities, risk_level=risk_level, data_quality_score=quality)
        assert 0.0 <= result["action_score"] <= 1.0
        assert 0.0 <= result["fault_probability"] <= 1.0
        assert 0.0 <= result["normalized_entropy"] <= 1.0
        assert 0.0 <= result["final_uncertainty"] <= 1.0


def test_result_is_deterministic() -> None:
    probabilities = _probs(0.7, 0.2, 0.1)
    first = _score(dict(probabilities), risk_level="medium", data_quality_score=0.8)
    second = _score(dict(probabilities), risk_level="medium", data_quality_score=0.8)

    assert first == second
