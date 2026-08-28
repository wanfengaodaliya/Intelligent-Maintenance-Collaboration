from __future__ import annotations

import pytest
from fastapi.responses import JSONResponse

import cloud_service.app as cloud_api
from cloud_service.config import CloudSettings
from cloud_service.device_arbitration.summary_contract import (
    adapt_summary_arbitration_request,
    attach_summary_identity,
)
from core.arbitration_contracts import ArbitrationValidationError
from core.diagnosis_identity import build_summary_window_id
from summary_service.aggregation import build_arbitration_request, build_window_result
from summary_service.contracts import normalize_bearing_result


ACTIONS = (
    "continue_operation",
    "enhanced_monitoring",
    "scheduled_inspection",
    "urgent_intervention",
    "shutdown",
)
RUN_ID = "run_01"
SUMMARY_WINDOW_ID = build_summary_window_id(
    device_id="machine_01",
    run_id=RUN_ID,
    window_start_sequence=17,
    window_end_sequence=17,
)

LEVEL_PROBS = {
    0: ({"healthy": 1.0, "outer_ring_damage": 0.0, "inner_ring_damage": 0.0}, "low", 0.0),
    1: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "low", 0.35),
    2: ({"healthy": 1 / 3, "outer_ring_damage": 1 / 3, "inner_ring_damage": 1 / 3}, "high", 0.55),
    3: ({"healthy": 0.0, "outer_ring_damage": 1.0, "inner_ring_damage": 0.0}, "high", 1.0),
}

LEGACY_GRADE = {0: 0, 1: 1, 2: 2, 3: 4}
SCORED_ACTION = {
    0: "continue_operation",
    1: "enhanced_monitoring",
    2: "scheduled_inspection",
    3: "shutdown",
}


def _bearing(bearing_id: str, edge_node_id: str, state: str, level: int) -> dict:
    suffix = bearing_id[-2:]
    probabilities, risk_level, score = LEVEL_PROBS[level]
    grade = LEGACY_GRADE[level]
    return {
        "result_id": f"edge_result_{suffix}",
        "device_id": "machine_01",
        "task_id": f"sd_{suffix}_tk_0001",
        "bearing_id": bearing_id,
        "sender_id": f"sender_{suffix}",
        "edge_node_id": edge_node_id,
        "run_id": RUN_ID,
        "window_start_sequence": 17,
        "window_end_sequence": 17,
        "bearing_state": state,
        "confidence": 0.9,
        "data_quality_score": 1.0,
        "risk_level": risk_level,
        "action_grade": grade,
        "recommended_action": ACTIONS[grade],
        "action_scorer_version": "action_scorer_v1",
        "action_score": score,
        "action_level": level,
        "scored_action": SCORED_ACTION[level],
        "scored_action_grade": grade,
        "class_probabilities": probabilities,
    }


def _request() -> dict:
    return {
        "conflict_id": "conflict_machine_01_17",
        "summary_result_id": "summary_machine_01_17",
        "summary_window_id": SUMMARY_WINDOW_ID,
        "device_id": "machine_01",
        "run_id": RUN_ID,
        "window_start_sequence": 17,
        "window_end_sequence": 17,
        "comparison": {
            "comparison_type": "action_level_gap_v1",
            "action_scorer_version": "action_scorer_v1",
            "conflict_level_gap_threshold": 3,
            "action_levels_by_edge": {"edge_01": 0, "edge_02": 3},
            "action_scores_by_edge": {"edge_01": 0.0, "edge_02": 1.0},
            "max_action_level_gap": 3,
            "max_action_score_gap": 1.0,
            "conflicting_pair_count": 1,
            "node_states": {"edge_01": "normal", "edge_02": "fault"},
            "state_mismatch": True,
            "state_mismatch_pair_count": 1,
            "max_cross_edge_grade_gap": 4,
        },
        "bearing_results": [
            _bearing("bearing_01", "edge_01", "normal", 0),
            _bearing("bearing_02", "edge_02", "fault", 3),
        ],
    }


_RECOMPUTED_COMPARISON = {
    "comparison_type": "action_level_gap_v1",
    "action_scorer_version": "action_scorer_v1",
    "conflict_level_gap_threshold": 3,
    "action_levels_by_edge": {"edge_01": 0, "edge_02": 3},
    "action_scores_by_edge": {"edge_01": 0.0, "edge_02": 1.0},
    "max_action_level_gap": 3,
    "max_action_score_gap": 1.0,
    "conflicting_pair_count": 1,
    "node_states": {"edge_01": "normal", "edge_02": "fault"},
    "state_mismatch": True,
    "state_mismatch_pair_count": 1,
    "max_cross_edge_grade_gap": 4,
}


def _settings(database_path):
    return CloudSettings("moment_light_adapt", "", "", "", 1.0, database_path)


def test_summary_contract_recomputes_conflict_and_adapts_existing_algorithm() -> None:
    adapted = adapt_summary_arbitration_request(_request())

    assert adapted["conflict_id"] == "conflict_machine_01_17"
    assert adapted["subject_id"] == "machine_01"
    assert adapted["task_id"] == "summary_machine_01_17"
    assert adapted["scenario_payload"]["bearing_results"][0]["recommended_action"] == (
        "continue_operation"
    )
    assert adapted["scenario_payload"]["bearing_results"][1]["recommended_action"] == (
        "shutdown"
    )
    assert adapted["summary_identity"]["comparison"] == _RECOMPUTED_COMPARISON
    attached = attach_summary_identity({"arbitration_id": "arb_01"}, adapted)
    assert attached["summary_result_id"] == "summary_machine_01_17"
    assert attached["window_start_sequence"] == 17


def test_same_state_with_level_gap_is_still_a_conflict() -> None:
    # Action-level conflict does not require a state mismatch.
    request = _request()
    request["bearing_results"][1] = _bearing("bearing_02", "edge_02", "normal", 3)
    request["comparison"]["node_states"] = {"edge_01": "normal", "edge_02": "normal"}
    request["comparison"]["state_mismatch"] = False
    request["comparison"]["state_mismatch_pair_count"] = 0

    adapted = adapt_summary_arbitration_request(request)

    assert adapted["summary_identity"]["comparison"]["state_mismatch"] is False
    assert adapted["summary_identity"]["comparison"]["max_action_level_gap"] == 3


def test_state_mismatch_with_small_level_gap_is_not_a_conflict() -> None:
    request = _request()
    request["bearing_results"][1] = _bearing("bearing_02", "edge_02", "fault", 1)
    request["comparison"]["action_levels_by_edge"] = {"edge_01": 0, "edge_02": 1}
    request["comparison"]["action_scores_by_edge"] = {"edge_01": 0.0, "edge_02": 0.35}
    request["comparison"]["max_action_level_gap"] = 1
    request["comparison"]["max_action_score_gap"] = 0.35
    request["comparison"]["conflicting_pair_count"] = 0
    request["comparison"]["max_cross_edge_grade_gap"] = 1

    with pytest.raises(ArbitrationValidationError, match="NOT_A_CONFLICT"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_non_binary_bearing_state() -> None:
    request = _request()
    request["bearing_results"][1]["bearing_state"] = "warning"
    request["comparison"]["node_states"]["edge_02"] = "warning"

    with pytest.raises(ArbitrationValidationError, match="bearing_state"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_a_false_reported_level_gap() -> None:
    request = _request()
    request["comparison"]["max_action_level_gap"] = 2

    with pytest.raises(ArbitrationValidationError, match="does not match"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_a_false_reported_score_gap() -> None:
    request = _request()
    request["comparison"]["max_action_score_gap"] = 0.5

    with pytest.raises(ArbitrationValidationError, match="does not match"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_action_level_that_does_not_match_score() -> None:
    request = _request()
    request["bearing_results"][1]["action_level"] = 2

    with pytest.raises(ArbitrationValidationError, match="action_level"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_scored_action_that_does_not_match_level() -> None:
    request = _request()
    request["bearing_results"][1]["scored_action"] = "enhanced_monitoring"

    with pytest.raises(ArbitrationValidationError, match="scored_action"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_false_reported_node_states() -> None:
    request = _request()
    request["comparison"]["node_states"] = {
        "edge_01": "fault",
        "edge_02": "normal",
    }

    with pytest.raises(ArbitrationValidationError, match="does not match"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_false_reported_state_mismatch() -> None:
    request = _request()
    request["comparison"]["state_mismatch"] = False

    with pytest.raises(ArbitrationValidationError, match="does not match"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_a_single_bearing_result() -> None:
    request = _request()
    request["bearing_results"] = request["bearing_results"][:1]

    with pytest.raises(ArbitrationValidationError, match="exactly two"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_three_bearing_results() -> None:
    request = _request()
    request["bearing_results"].append(
        _bearing("bearing_03", "edge_01", "normal", 1)
    )

    with pytest.raises(ArbitrationValidationError, match="exactly two"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_bearing_03_replacing_bearing_02() -> None:
    request = _request()
    request["bearing_results"][1] = _bearing("bearing_03", "edge_02", "fault", 3)

    with pytest.raises(ArbitrationValidationError, match="bearing_01 and bearing_02"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_requires_two_bearings_from_two_edges() -> None:
    request = _request()
    request["bearing_results"][1]["edge_node_id"] = "edge_01"

    with pytest.raises(ArbitrationValidationError, match="edge_01 and edge_02"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_duplicate_bearing_ids() -> None:
    request = _request()
    request["bearing_results"][1]["bearing_id"] = "bearing_01"

    with pytest.raises(ArbitrationValidationError, match="bearing_id values must be unique"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_rejects_duplicate_result_ids() -> None:
    request = _request()
    request["bearing_results"][1]["result_id"] = "edge_result_01"

    with pytest.raises(ArbitrationValidationError, match="result_id values must be unique"):
        adapt_summary_arbitration_request(request)


def test_summary_contract_requires_data_quality_score() -> None:
    request = _request()
    request["bearing_results"][0].pop("data_quality_score")

    with pytest.raises(ArbitrationValidationError, match="data_quality_score"):
        adapt_summary_arbitration_request(request)


def test_cloud_endpoint_persists_summary_identity_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))

    first = cloud_api.device_arbitration(_request())
    repeated = cloud_api.device_arbitration(_request())

    assert isinstance(first, dict)
    assert repeated == first
    assert first["conflict_id"] == "conflict_machine_01_17"
    assert first["summary_result_id"] == "summary_machine_01_17"
    assert first["bearing_result_ids"] == [
        "edge_result_01",
        "edge_result_02",
    ]


def test_cloud_endpoint_returns_409_for_same_conflict_id_with_new_payload(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))
    assert isinstance(cloud_api.device_arbitration(_request()), dict)
    changed = _request()
    changed["bearing_results"][0]["confidence"] = 0.7

    response = cloud_api.device_arbitration(changed)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409


def test_cloud_arbitration_returns_resolved_or_manual_review(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))

    result = cloud_api.device_arbitration(_request())

    assert isinstance(result, dict)
    assert result["status"] in {"resolved", "manual_review"}
    assert result["conflict_id"] == "conflict_machine_01_17"
    assert result["summary_result_id"] == "summary_machine_01_17"


def _summary_bearing(bearing_id: str, edge_node_id: str, state: str, level: int) -> dict:
    payload = dict(_bearing(bearing_id, edge_node_id, state, level))
    payload.update(
        {
            "decision_round_id": f"round_{bearing_id[-2:]}",
            "model_version": "model-test",
            "created_at_ns": 100,
        }
    )
    return payload


def test_summary_built_two_bearing_request_is_accepted_by_cloud_contract(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud_api, "load_cloud_settings", lambda: _settings(tmp_path / "cloud.db"))
    source_results = [
        normalize_bearing_result(_summary_bearing("bearing_01", "edge_01", "normal", 0)),
        normalize_bearing_result(_summary_bearing("bearing_02", "edge_02", "fault", 3)),
    ]
    window_result = build_window_result(
        source_results,
        closed_at_ns=1_000,
        expected_bearing_ids=("bearing_01", "bearing_02"),
    )
    assert window_result["has_conflict"] is True

    request = build_arbitration_request(window_result)

    adapted = adapt_summary_arbitration_request(request)
    assert adapted["summary_identity"]["comparison"] == _RECOMPUTED_COMPARISON

    response = cloud_api.device_arbitration(request)
    assert isinstance(response, dict)
    assert response["status"] in {"resolved", "manual_review"}
    assert response["comparison"] == _RECOMPUTED_COMPARISON
