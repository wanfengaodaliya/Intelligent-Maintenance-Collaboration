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
