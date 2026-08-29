from __future__ import annotations

import pytest

from summary_service.contracts import (
    BINARY_BEARING_STATES,
    EXPECTED_BEARING_IDS,
    EXPECTED_EDGE_NODE_IDS,
    build_summary_window_id,
    group_key,
    normalize_bearing_result,
)


def payload(**overrides) -> dict:
    base = {
        "result_id": "result_01",
        "device_id": "machine_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "edge_node_id": "edge_01",
        "run_id": "run_01",
        "decision_round_id": "round_01",
        "window_start_sequence": 1,
        "window_end_sequence": 3,
        "bearing_state": "normal",
        "risk_level": "low",
        "confidence": 0.9,
        "data_quality_score": 0.8,
        "model_version": "model-test",
        "created_at_ns": 100,
        "class_probabilities": {
            "healthy": 1.0,
            "outer_ring_damage": 0.0,
            "inner_ring_damage": 0.0,
        },
    }
    base.update(overrides)
    return base


def test_binary_state_constants_align_with_contract() -> None:
    assert BINARY_BEARING_STATES == {"normal", "fault"}
    assert EXPECTED_BEARING_IDS == ("bearing_01", "bearing_02")
    assert EXPECTED_EDGE_NODE_IDS == ("edge_01", "edge_02")


@pytest.mark.parametrize("state", ["normal", "fault"])
def test_accepts_binary_bearing_states(state: str) -> None:
    result = normalize_bearing_result(payload(bearing_state=state))

    assert result["bearing_state"] == state


@pytest.mark.parametrize("state", ["warning", "abnormal", "unknown", "NORMAL", ""])
def test_rejects_non_binary_bearing_states(state: str) -> None:
    with pytest.raises(ValueError, match="normal or fault"):
        normalize_bearing_result(payload(bearing_state=state))


def test_rejects_missing_bearing_state() -> None:
    raw = payload()
    del raw["bearing_state"]

    with pytest.raises(ValueError, match="bearing_state"):
        normalize_bearing_result(raw)


def test_rejects_state_derived_from_action_transformation() -> None:
    # The contract must never accept an action-string as a state: it must be
    # declared explicitly as normal/fault.
    with pytest.raises(ValueError, match="normal or fault"):
        normalize_bearing_result(payload(bearing_state="shutdown"))


def test_preserves_model_audit_fields_without_conflict_role() -> None:
    result = normalize_bearing_result(
        payload(
            diagnosis_label="outer_ring_damage",
            class_probabilities={
                "healthy": 0.1,
                "inner_ring_damage": 0.2,
                "outer_ring_damage": 0.7,
            },
        )
    )

    assert result["diagnosis_label"] == "outer_ring_damage"
    assert result["class_probabilities"]["outer_ring_damage"] == 0.7
    # Audit fields never leak into the window identity.
    assert "diagnosis_label" not in build_summary_window_id(
        "machine_01", None, 1, 3
    )


def test_rejects_invalid_class_probabilities() -> None:
    with pytest.raises(ValueError, match="class_probabilities"):
        normalize_bearing_result(
            payload(class_probabilities={"healthy": 1.5})
        )


@pytest.mark.parametrize("run_id", [None, "", "   ", 123])
def test_run_id_is_required_and_non_empty(run_id) -> None:
    with pytest.raises(ValueError, match="run_id"):
        normalize_bearing_result(payload(run_id=run_id))


def test_missing_run_id_is_rejected() -> None:
    raw = payload()
    del raw["run_id"]

    with pytest.raises(ValueError, match="run_id"):
        normalize_bearing_result(raw)


def test_run_id_is_normalized() -> None:
    assert normalize_bearing_result(payload(run_id=" run_01 "))["run_id"] == "run_01"


def test_summary_window_id_is_stable_and_run_scoped() -> None:
    base = build_summary_window_id("machine_01", None, 1, 3)
    assert base == build_summary_window_id("machine_01", None, 1, 3)
    assert base != build_summary_window_id("machine_01", None, 2, 3)
    assert base != build_summary_window_id("machine_02", None, 1, 3)
    # Different runs never share a window identity, even for equal sequences.
    assert base != build_summary_window_id("machine_01", "run_01", 1, 3)
    assert build_summary_window_id("machine_01", "run_01", 1, 3) == (
        build_summary_window_id("machine_01", "run_01", 1, 3)
    )


def test_normalized_result_carries_summary_window_id() -> None:
    result = normalize_bearing_result(payload(run_id="run_01"))

    assert result["summary_window_id"] == build_summary_window_id(
        "machine_01", "run_01", 1, 3
    )
    assert group_key(result) == result["summary_window_id"]


def test_two_senders_of_one_window_share_group_key() -> None:
    left = normalize_bearing_result(payload(result_id="r1", bearing_id="bearing_01"))
    right = normalize_bearing_result(
        payload(
            result_id="r2",
            bearing_id="bearing_02",
            edge_node_id="edge_02",
            sender_id="sender_02",
            task_id="sd_02_tk_0001",
            decision_round_id="round_02",
        )
    )

    assert group_key(left) == group_key(right)


def test_rejects_out_of_range_numeric_fields() -> None:
    with pytest.raises(ValueError, match="confidence"):
        normalize_bearing_result(payload(confidence=1.5))
    with pytest.raises(ValueError, match="window_end_sequence"):
        normalize_bearing_result(
            payload(window_start_sequence=5, window_end_sequence=4)
        )


def test_missing_class_probabilities_is_rejected() -> None:
    raw = payload()
    del raw["class_probabilities"]

    with pytest.raises(ValueError, match="class_probabilities are required"):
        normalize_bearing_result(raw)


def test_class_probabilities_must_have_exactly_three_labels() -> None:
    with pytest.raises(ValueError, match="exactly the three H5 labels"):
        normalize_bearing_result(
            payload(
                class_probabilities={
                    "healthy": 0.9,
                    "outer_ring_damage": 0.05,
                    "inner_ring_damage": 0.05,
                    "extra": 0.0,
                }
            )
        )


@pytest.mark.parametrize(
    "probabilities",
    [
        {"healthy": -0.1, "outer_ring_damage": 0.5, "inner_ring_damage": 0.6},
        {"healthy": 0.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0},
        {"healthy": 0.5, "outer_ring_damage": 0.5, "inner_ring_damage": 0.5},
        {"healthy": float("nan"), "outer_ring_damage": 0.5, "inner_ring_damage": 0.5},
        {"healthy": float("inf"), "outer_ring_damage": 0.0, "inner_ring_damage": 0.0},
    ],
)
def test_rejects_invalid_class_probability_values(probabilities) -> None:
    with pytest.raises(ValueError, match="class probab"):
        normalize_bearing_result(payload(class_probabilities=probabilities))


@pytest.mark.parametrize("risk_level", ["extreme", "", "LOW"])
def test_rejects_invalid_risk_level(risk_level: str) -> None:
    with pytest.raises(ValueError, match="risk_level"):
        normalize_bearing_result(payload(risk_level=risk_level))


def test_rejects_boolean_data_quality_score() -> None:
    with pytest.raises(ValueError, match="data_quality_score"):
        normalize_bearing_result(payload(data_quality_score=True))


def test_scoring_fields_are_merged_into_normalized_result() -> None:
    result = normalize_bearing_result(payload())

    assert result["action_scorer_version"] == "action_scorer_v1"
    assert result["action_level"] == 0
    assert result["scored_action"] == "continue_operation"
    assert set(result["normalized_class_probabilities"]) == {
        "healthy",
        "outer_ring_damage",
        "inner_ring_damage",
    }
    # Raw probabilities are preserved verbatim.
    assert result["class_probabilities"]["healthy"] == 1.0
