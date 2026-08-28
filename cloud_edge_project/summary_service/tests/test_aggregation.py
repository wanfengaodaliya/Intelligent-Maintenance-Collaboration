from __future__ import annotations

import pytest

from summary_service.aggregation import (
    build_arbitration_request,
    build_incomplete_window_result,
    build_window_result,
)
from summary_service.contracts import normalize_bearing_result


ACTIONS = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "urgent_intervention",
    4: "shutdown",
}

LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low"),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low"),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high"),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high"),
}

LEGACY_GRADE = {0: 0, 1: 1, 2: 2, 3: 4}


def bearing(
    bearing_id: str,
    edge_node_id: str,
    state: str,
    action_level: int,
    *,
    run_id: str | None = None,
) -> dict:
    suffix = bearing_id[-2:]
    probabilities, risk_level = LEVEL_PROBS[action_level]
    grade = LEGACY_GRADE[action_level]
    return normalize_bearing_result(
        {
            "result_id": f"result_{suffix}",
            "device_id": "machine_01",
            "task_id": f"sd_{suffix}_tk_0001",
            "bearing_id": bearing_id,
            "sender_id": f"sender_{suffix}",
            "edge_node_id": edge_node_id,
            "decision_round_id": f"round_{suffix}",
            "window_start_sequence": 1,
            "window_end_sequence": 3,
            "bearing_state": state,
            "risk_level": risk_level,
            "action_grade": grade,
            "recommended_action": ACTIONS[grade],
            "confidence": 0.9,
            "data_quality_score": 1.0,
            "model_version": "model-test",
            "created_at_ns": 100,
            "run_id": run_id,
            "class_probabilities": probabilities,
        }
    )


def test_normal_normal_is_final_with_final_state() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0),
         bearing("bearing_02", "edge_02", "normal", 1)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "FINAL"
    assert result["has_conflict"] is False
    assert result["state_mismatch"] is False
    assert result["node_states"] == {"edge_01": "normal", "edge_02": "normal"}
    assert result["final_state"] == "normal"
    assert result["arbitration_status"] is None
    assert result["final_action_level"] == 1
    assert result["final_action_grade"] == 1
    assert result["recommended_action"] == "enhanced_monitoring"


def test_fault_fault_is_final_with_final_state() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "fault", 2),
         bearing("bearing_02", "edge_02", "fault", 3)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "FINAL"
    assert result["has_conflict"] is False
    assert result["final_state"] == "fault"
    assert result["final_action_level"] == 3
    assert result["final_action_grade"] == 4
    assert result["recommended_action"] == "shutdown"


@pytest.mark.parametrize(
    ("left_level", "right_level", "conflict"),
    [
        (0, 1, False),
        (0, 2, False),
        (0, 3, True),
        (1, 2, False),
        (1, 3, False),
        (2, 3, False),
    ],
)
def test_action_level_gap_decides_conflict(
    left_level: int, right_level: int, conflict: bool
) -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", left_level),
         bearing("bearing_02", "edge_02", "normal", right_level)],
        closed_at_ns=1_000,
    )

    assert result["has_conflict"] is conflict
    assert result["conflict_pair_count"] == (1 if conflict else 0)
    assert result["max_action_level_gap"] == abs(left_level - right_level)
    assert result["result_status"] == (
        "PENDING_ARBITRATION" if conflict else "FINAL"
    )


def test_state_mismatch_with_small_level_gap_is_not_a_conflict() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 1),
         bearing("bearing_02", "edge_02", "fault", 2)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "FINAL"
    assert result["has_conflict"] is False
    assert result["state_mismatch"] is True
    assert result["state_mismatch_pair_count"] == 1
    # Conservative summary: any fault source makes the window fault.
    assert result["final_state"] == "fault"
    assert result["final_action_level"] == 2
    assert result["recommended_action"] == "scheduled_inspection"


def test_same_state_with_large_level_gap_is_a_conflict() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0),
         bearing("bearing_02", "edge_02", "normal", 3)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "PENDING_ARBITRATION"
    assert result["has_conflict"] is True
    assert result["state_mismatch"] is False
    assert result["state_mismatch_pair_count"] == 0
    assert result["final_state"] is None
    assert result["final_action_level"] is None


def test_score_gap_is_recorded_but_does_not_decide_conflict() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0),
         bearing("bearing_02", "edge_02", "normal", 3)],
        closed_at_ns=1_000,
    )

    assert result["max_action_score_gap"] > 0.0
    assert result["has_conflict"] is True  # level gap 3, not score gap


def test_two_results_from_the_same_node_are_incomplete() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0),
         bearing("bearing_02", "edge_01", "fault", 3)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "INCOMPLETE"
    assert result["has_conflict"] is False
    assert result["state_mismatch"] is False
    assert result["final_state"] is None
    assert result["incomplete_reason"] == "INSUFFICIENT_EDGE_DIVERSITY"
    assert result["excluded_from_formal_metrics"] is True
    assert result["final_action_level"] is None


def test_missing_bearing_result_closes_incomplete() -> None:
    result = build_incomplete_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0)],
        closed_at_ns=1_000,
    )

    assert result["result_status"] == "INCOMPLETE"
    assert result["missing_bearing_ids"] == ["bearing_02"]
    assert result["has_conflict"] is False
    assert result["state_mismatch"] is False
    assert result["final_state"] is None
    assert result["excluded_from_formal_metrics"] is True
    assert result["final_action_level"] is None
    assert result["recommended_action"] is None


def test_window_identity_groups_by_shared_summary_window() -> None:
    results = [
        bearing("bearing_01", "edge_01", "normal", 0, run_id="run_01"),
        bearing("bearing_02", "edge_02", "normal", 0, run_id="run_01"),
    ]
    result = build_window_result(results, closed_at_ns=1_000)

    assert result["summary_window_id"] == results[0]["summary_window_id"]
    assert result["run_id"] == "run_01"


def test_run_scoped_results_refuse_to_share_one_window() -> None:
    with pytest.raises(ValueError, match="same device window"):
        build_window_result(
            [bearing("bearing_01", "edge_01", "normal", 0, run_id="run_01"),
             bearing("bearing_02", "edge_02", "normal", 0, run_id="run_02")],
            closed_at_ns=1_000,
        )


def test_bearing_set_must_match_expected_bearings() -> None:
    with pytest.raises(ValueError, match="one result for each bearing"):
        build_window_result(
            [bearing("bearing_01", "edge_01", "normal", 0),
             bearing("bearing_01", "edge_02", "fault", 3)],
            closed_at_ns=1_000,
        )


def test_arbitration_request_reports_action_level_comparison() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "normal", 0),
         bearing("bearing_02", "edge_02", "fault", 3)],
        closed_at_ns=1_000,
    )

    request = build_arbitration_request(result)

    assert request["comparison"]["comparison_type"] == "action_level_gap_v1"
    assert request["comparison"]["action_scorer_version"] == "action_scorer_v1"
    assert request["comparison"]["conflict_level_gap_threshold"] == 3
    assert request["comparison"]["action_levels_by_edge"] == {
        "edge_01": 0,
        "edge_02": 3,
    }
    assert request["comparison"]["max_action_level_gap"] == 3
    assert request["comparison"]["conflicting_pair_count"] == 1
    assert request["comparison"]["node_states"] == {
        "edge_01": "normal",
        "edge_02": "fault",
    }
    assert request["comparison"]["state_mismatch"] is True
    assert request["summary_window_id"] == result["summary_window_id"]
    assert len(request["bearing_results"]) == 2


def test_arbitration_request_refuses_non_conflicting_window() -> None:
    result = build_window_result(
        [bearing("bearing_01", "edge_01", "fault", 3),
         bearing("bearing_02", "edge_02", "fault", 2)],
        closed_at_ns=1_000,
    )

    with pytest.raises(ValueError, match="conflicted windows"):
        build_arbitration_request(result)
