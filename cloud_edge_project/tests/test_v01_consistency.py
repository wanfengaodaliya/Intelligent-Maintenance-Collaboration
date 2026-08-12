import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import start_all
from consistency_service import app as consistency_app
from consistency_service.resolver import resolve_decisions


CONFLICT_REQUEST = {
    "decision_id": "decision_001",
    "scenario": "energy",
    "decisions": [
        {
            "task_id": "task_A",
            "source_node": "edge_1",
            "target_device": "battery_01",
            "action": "charge",
            "power_kw": 80,
            "confidence": 0.85,
            "priority": 0.9,
            "timestamp": "2026-06-20 10:00:01",
        },
        {
            "task_id": "task_B",
            "source_node": "edge_2",
            "target_device": "battery_01",
            "action": "discharge",
            "power_kw": 60,
            "confidence": 0.78,
            "priority": 0.7,
            "timestamp": "2026-06-20 10:00:02",
        },
    ],
    "global_constraints": {
        "battery_01_max_power_kw": 100,
        "allow_charge_and_discharge_same_time": False,
    },
}


def test_opposite_actions_choose_highest_priority_decision():
    result = resolve_decisions(CONFLICT_REQUEST)

    assert result == {
        "decision_id": "decision_001",
        "has_conflict": True,
        "conflict_type": "opposite_action",
        "final_action": "charge",
        "selected_source_node": "edge_1",
        "reason": "charge decision has higher priority and confidence",
        "resolved": True,
    }


def test_power_overload_chooses_highest_priority_decision():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [
            {**CONFLICT_REQUEST["decisions"][0], "power_kw": 60, "priority": 0.7},
            {**CONFLICT_REQUEST["decisions"][1], "action": "charge", "power_kw": 50, "priority": 0.8},
        ],
    }

    result = resolve_decisions(request)

    assert result["has_conflict"] is True
    assert result["conflict_type"] == "power_overload"
    assert result["selected_source_node"] == "edge_2"
    assert result["final_action"] == "charge"
    assert result["resolved"] is True


def test_allowed_opposite_actions_do_not_combine_power_for_overload():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [
            {**CONFLICT_REQUEST["decisions"][0], "power_kw": 60},
            {**CONFLICT_REQUEST["decisions"][1], "power_kw": 60},
        ],
        "global_constraints": {
            "battery_01_max_power_kw": 100,
            "allow_charge_and_discharge_same_time": True,
        },
    }

    result = resolve_decisions(request)

    assert result["has_conflict"] is False
    assert result["conflict_type"] is None
    assert result["selected_source_node"] == "edge_1"


def test_opposite_action_uses_first_conflicting_device_not_unrelated_global_maximum():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [
            {**CONFLICT_REQUEST["decisions"][0], "priority": 0.7},
            {**CONFLICT_REQUEST["decisions"][1], "priority": 0.8},
            {
                **CONFLICT_REQUEST["decisions"][0],
                "source_node": "edge_3",
                "target_device": "battery_02",
                "action": "charge",
                "power_kw": 10,
                "priority": 1.0,
            },
        ],
        "global_constraints": {
            "battery_01_max_power_kw": 100,
            "battery_02_max_power_kw": 100,
            "allow_charge_and_discharge_same_time": False,
        },
    }

    result = resolve_decisions(request)

    assert result["conflict_type"] == "opposite_action"
    assert result["selected_source_node"] == "edge_2"
    assert result["final_action"] == "discharge"
    assert result["reason"] == "discharge decision has highest priority; confidence is used only to break priority ties"


def test_opposite_action_takes_precedence_over_earlier_power_overload():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [
            {**CONFLICT_REQUEST["decisions"][0], "action": "charge", "power_kw": 60, "priority": 0.8},
            {**CONFLICT_REQUEST["decisions"][1], "action": "charge", "power_kw": 60, "priority": 0.7},
            {
                **CONFLICT_REQUEST["decisions"][0],
                "source_node": "edge_3",
                "target_device": "battery_02",
                "action": "charge",
                "power_kw": 10,
                "priority": 0.2,
            },
            {
                **CONFLICT_REQUEST["decisions"][1],
                "source_node": "edge_4",
                "target_device": "battery_02",
                "action": "discharge",
                "power_kw": 10,
                "priority": 0.3,
            },
        ],
        "global_constraints": {
            "battery_01_max_power_kw": 100,
            "battery_02_max_power_kw": 100,
            "allow_charge_and_discharge_same_time": False,
        },
    }

    result = resolve_decisions(request)

    assert result["conflict_type"] == "opposite_action"
    assert result["selected_source_node"] == "edge_4"
    assert result["final_action"] == "discharge"


def test_first_device_wins_when_multiple_devices_have_opposite_actions():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [
            {**CONFLICT_REQUEST["decisions"][0], "priority": 0.8},
            {**CONFLICT_REQUEST["decisions"][1], "priority": 0.7},
            {
                **CONFLICT_REQUEST["decisions"][0],
                "source_node": "edge_3",
                "target_device": "battery_02",
                "action": "charge",
                "priority": 1.0,
            },
            {
                **CONFLICT_REQUEST["decisions"][1],
                "source_node": "edge_4",
                "target_device": "battery_02",
                "action": "discharge",
                "priority": 0.9,
            },
        ],
        "global_constraints": {
            "battery_01_max_power_kw": 100,
            "battery_02_max_power_kw": 100,
            "allow_charge_and_discharge_same_time": False,
        },
    }

    result = resolve_decisions(request)

    assert result["conflict_type"] == "opposite_action"
    assert result["selected_source_node"] == "edge_1"
    assert result["final_action"] == "charge"


def test_non_conflicting_decisions_select_highest_priority_decision():
    request = {
        **CONFLICT_REQUEST,
        "decisions": [CONFLICT_REQUEST["decisions"][0]],
    }

    result = resolve_decisions(request)

    assert result["has_conflict"] is False
    assert result["conflict_type"] is None
    assert result["final_action"] == "charge"
    assert result["selected_source_node"] == "edge_1"
    assert result["resolved"] is True


def test_single_non_conflicting_decision_reason_does_not_claim_comparison():
    result = resolve_decisions({**CONFLICT_REQUEST, "decisions": [CONFLICT_REQUEST["decisions"][0]]})

    assert result["reason"] == "only applicable decision was selected"


def test_single_power_overload_returns_no_unsafe_executable_action():
    result = resolve_decisions(
        {
            **CONFLICT_REQUEST,
            "decisions": [{**CONFLICT_REQUEST["decisions"][0], "power_kw": 101}],
        }
    )

    assert result["conflict_type"] == "power_overload"
    assert result["reason"] == "no individually safe decision satisfies the target power limit"
    assert result["final_action"] is None
    assert result["selected_source_node"] is None
    assert result["resolved"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {**CONFLICT_REQUEST, "decision_id": ""},
        {**CONFLICT_REQUEST, "decisions": []},
        {**CONFLICT_REQUEST, "decisions": [{**CONFLICT_REQUEST["decisions"][0], "action": "hold"}]},
        {**CONFLICT_REQUEST, "decisions": [{**CONFLICT_REQUEST["decisions"][0], "power_kw": -1}]},
        {**CONFLICT_REQUEST, "global_constraints": {"battery_01_max_power_kw": -1, "allow_charge_and_discharge_same_time": False}},
        {**CONFLICT_REQUEST, "global_constraints": {"battery_01_max_power_kw": 100, "allow_charge_and_discharge_same_time": "false"}},
    ],
)
def test_invalid_consistency_requests_return_http_400(payload):
    response = TestClient(consistency_app.app).post("/consistency/resolve", json=payload)

    assert response.status_code == 400


@pytest.mark.parametrize("payload", [None, [], {"decision_id": "decision_001"}, {**CONFLICT_REQUEST, "decisions": [{key: value for key, value in CONFLICT_REQUEST["decisions"][0].items() if key != "action"}]}, {**CONFLICT_REQUEST, "decisions": [{**CONFLICT_REQUEST["decisions"][0], "action": []}]}])
def test_malformed_json_values_return_http_400_not_framework_or_type_errors(payload):
    response = TestClient(consistency_app.app).post("/consistency/resolve", json=payload)

    assert response.status_code == 400


def test_missing_consistency_request_body_returns_http_400():
    response = TestClient(consistency_app.app).post("/consistency/resolve")

    assert response.status_code == 400


def test_health_uses_consistency_port_from_configuration(monkeypatch):
    monkeypatch.setattr(
        consistency_app,
        "load_config",
        lambda: {"services": {"consistency": {"host": "10.0.0.5", "port": 9125}}},
    )

    response = TestClient(consistency_app.app).get("/health")

    assert response.status_code == 200
    assert response.json()["port"] == 9125


def test_start_all_launches_configured_consistency_service(monkeypatch):
    commands = []

    class Process:
        pass

    monkeypatch.setattr(start_all.subprocess, "Popen", lambda command: commands.append(command) or Process())
    service = next(service for service in start_all.SERVICES if service.name == "consistency_service")

    start_all.start_service(service, {"services": {"consistency": {"host": "10.0.0.5", "port": 9125}}})

    assert commands == [
        [sys.executable, "-m", "uvicorn", "consistency_service.app:app", "--host", "10.0.0.5", "--port", "9125"]
    ]


def test_v01_examples_are_valid_json_and_keep_task_contracts():
    examples = Path(__file__).resolve().parents[1] / "examples"
    task = json.loads((examples / "task_industrial.json").read_text(encoding="utf-8"))
    edge = json.loads((examples / "edge_result.json").read_text(encoding="utf-8"))
    schedule = json.loads((examples / "schedule_decision.json").read_text(encoding="utf-8"))

    assert task["task_id"] == edge["task_id"] == schedule["task_id"] == "task_0001"
    assert {"scenario", "source_node", "task_type", "data"}.issubset(task)
    assert {"node_id", "label", "confidence", "risk_level", "edge_latency_ms", "need_cloud"}.issubset(edge)
    assert schedule["route"] in {"edge", "fog", "cloud", "fallback_edge", "edge_cloud"}
