import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# AUD-12: these end-to-end tests import the Edge application, which loads the
# H5 diagnostic model (torch). Run them in the Conda "moment" environment;
# torch-less .venv runs skip them explicitly.
pytest.importorskip(
    "torch",
    reason="V0.1 e2e tests import the torch-based Edge app (Conda moment env)",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_service import app as cloud_app
from common import logger
from common.config import DEFAULT_CONFIG
from common.schemas import ContractError, validate_task_log
from consistency_service import app as consistency_app
from consistency_service.resolver import resolve_decisions
from edge_service import app as edge_app
from edge_service.model import infer_edge_v01
from log_service import app as log_app
from scheduler import api as scheduler_api
from scheduler import rule_scheduler
from scheduler.rule_scheduler import PreDDPGScheduler, decide_schedule


TASK = {
    "task_id": "task_0001",
    "scenario": "industrial",
    "source_node": "edge_1",
    "task_type": "fault_detection",
    "timestamp": "2026-06-20 10:00:00",
    "deadline_ms": 200,
    "priority": 0.8,
    "data_size_kb": 128,
    "data": {
        "device_id": "machine_01",
        "temperature": 78.5,
        "vibration": 0.63,
        "current": 1.0,
        "load": 0.4,
    },
}


def v01_schedule_request() -> dict:
    return {
        "task": {
            "task_id": "task_0001",
            "scenario": "industrial",
            "task_type": "fault_detection",
            "deadline_ms": 200,
            "priority": 0.8,
            "data_size_kb": 128,
        },
        "edge_result": {
            "task_id": "task_0001",
            "label": "fault",
            "confidence": 0.72,
            "edge_latency_ms": 38,
            "need_cloud": True,
        },
        "network_state": {
            "latency_ms": 30,
            "bandwidth_mbps": 20,
            "packet_loss": 0.01,
            "cloud_available": True,
        },
        "node_state": {
            "edge_cpu_usage": 0.55,
            "edge_memory_usage": 0.62,
            "cloud_queue_length": 3,
            "fog_available": True,
        },
    }


def task_log(**overrides) -> dict:
    value = {
        "task_id": "task_0001",
        "scenario": "industrial",
        "source_node": "edge_1",
        "route": "cloud",
        "edge_latency_ms": 38,
        "network_latency_ms": 30,
        "cloud_latency_ms": 86,
        "total_latency_ms": 154,
        "edge_confidence": 0.72,
        "cloud_confidence": 0.93,
        "final_label": "fault",
        "final_confidence": 0.93,
        "success": True,
        "has_conflict": False,
        "conflict_resolved": None,
        "timestamp": "2026-06-20 10:00:05",
    }
    value.update(overrides)
    return value


def consistency_request(*decisions: dict, max_power_kw: float = 100) -> dict:
    return {
        "decision_id": "decision_001",
        "scenario": "energy",
        "decisions": list(decisions),
        "global_constraints": {
            "battery_01_max_power_kw": max_power_kw,
            "allow_charge_and_discharge_same_time": False,
        },
    }


def energy_decision(source: str, power: float, priority: float) -> dict:
    return {
        "task_id": f"task_{source}",
        "source_node": source,
        "target_device": "battery_01",
        "action": "charge",
        "power_kw": power,
        "confidence": 0.8,
        "priority": priority,
        "timestamp": "2026-06-20 10:00:01",
    }


@pytest.mark.parametrize(
    ("client", "path"),
    [
        (TestClient(edge_app.app), "/edge/infer"),
        (TestClient(cloud_app.app), "/cloud/infer"),
        (TestClient(scheduler_api.app), "/scheduler/decide"),
        (TestClient(consistency_app.app), "/consistency/resolve"),
        (TestClient(log_app.app), "/logs/task_trace"),
    ],
)
@pytest.mark.parametrize("body", [None, []])
def test_public_service_bodies_map_missing_and_non_object_json_to_400(client, path, body):
    response = client.post(path) if body is None else client.post(path, json=body)

    assert response.status_code == 400
    assert "error_code" in response.json()


def test_malformed_edge_task_id_uses_v01_validation_not_legacy_packet_validation():
    response = TestClient(edge_app.app).post("/edge/infer", json={"task_id": "task_0001"})

    assert response.status_code == 400
    assert response.json()["message"] == "missing field: scenario"


def test_malformed_cloud_task_id_uses_v01_validation_not_legacy_analysis():
    response = TestClient(cloud_app.app).post("/cloud/infer", json={"task_id": "task_0001"})

    assert response.status_code == 400
    assert response.json()["message"] == "missing field: scenario"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("edge_result", "need_cloud"), 1, "edge_result.need_cloud must be boolean"),
        (("network_state", "packet_loss"), float("nan"), "network_state.packet_loss must be a finite number"),
        (("node_state", "edge_cpu_usage"), 1.1, "node_state.edge_cpu_usage must be between 0 and 1"),
        (("node_state", "cloud_queue_length"), -1, "node_state.cloud_queue_length must be non-negative"),
        (("task", "data_size_kb"), -1, "task.data_size_kb must be non-negative"),
        (("edge_result", "task_id"), "task_other", "edge_result.task_id must match task.task_id"),
    ],
)
def test_v01_scheduler_rejects_invalid_nested_contract(path, value, message):
    request = v01_schedule_request()
    request[path[0]][path[1]] = value

    response = TestClient(scheduler_api.app).post(
        "/scheduler/decide",
        content=json.dumps(request),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["message"] == message


def test_any_v01_scheduler_nested_key_selects_v01_validation():
    response = TestClient(scheduler_api.app).post(
        "/scheduler/decide", json={"network_state": {"cloud_available": True}}
    )

    assert response.status_code == 400
    assert response.json()["message"] == "missing field: task"


def test_stdlib_scheduler_error_mapping_treats_contract_errors_as_400():
    status, payload = scheduler_api._error_payload(ContractError("MISSING_FIELD", "missing field: task"))

    assert status == 400
    assert payload["error_code"] == "MISSING_FIELD"


def test_legacy_preddpg_keeps_packet_loss_fallback_and_internal_fields():
    request = v01_schedule_request()
    request["network_state"]["packet_loss"] = 0.5

    result = decide_schedule(request)

    assert result["route"] == "fallback_edge"
    assert result["scheduler"] == "PER-DDPG-rule-minimal"
    assert set(result["policy_score"]) == {"edge", "cloud"}


def test_preddpg_configuration_and_v01_projection_remain_separate():
    request = v01_schedule_request()

    assert PreDDPGScheduler(max_packet_loss=0.005).decide(request).route == "fallback_edge"
    assert set(rule_scheduler.decide_schedule_v01(request)) == {
        "task_id",
        "route",
        "target_node",
        "reason",
        "estimated_total_latency_ms",
        "upload_required",
    }


def test_task_request_accepts_energy_business_data_without_industrial_sensors():
    result = infer_edge_v01(
        {
            **TASK,
            "scenario": "energy",
            "task_type": "energy_dispatch",
            "data": {"meter_id": "meter_01", "active_power_kw": 12.5},
        }
    )

    assert (result["label"], result["confidence"], result["risk_level"], result["need_cloud"]) == (
        "normal",
        0.5,
        "low",
        True,
    )


@pytest.mark.parametrize("scenario", ["", "bearing", "Industrial"])
def test_task_request_accepts_exactly_industrial_and_energy(scenario):
    with pytest.raises(ContractError, match="scenario must be industrial or energy"):
        infer_edge_v01({**TASK, "scenario": scenario})


def test_energy_task_requires_a_non_empty_business_data_object():
    with pytest.raises(ContractError, match="data must be a non-empty object"):
        infer_edge_v01({**TASK, "scenario": "energy", "data": {}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_latency_ms": float("nan")},
        {"edge_confidence": float("inf")},
        {"route": "cloud", "cloud_latency_ms": None},
        {"route": "cloud", "cloud_confidence": None},
        {"route": "edge", "cloud_latency_ms": 1, "cloud_confidence": 0.9},
        {"has_conflict": False, "conflict_resolved": False},
        {"has_conflict": True, "conflict_resolved": None},
    ],
)
def test_task_log_rejects_nonfinite_and_semantically_inconsistent_values(overrides):
    with pytest.raises(ContractError):
        validate_task_log(task_log(**overrides))


def test_jsonl_writer_forbids_nonfinite_values_even_after_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(logger, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(logger, "validate_task_log", lambda trace: trace)

    with pytest.raises(ValueError, match="Out of range float values"):
        logger.append_task_trace(task_log(total_latency_ms=float("nan")), {"log": {"path": "trace.jsonl"}})


def test_single_unsafe_decision_returns_no_executable_action():
    result = resolve_decisions(consistency_request(energy_decision("edge_1", 101, 0.9)))

    assert result["has_conflict"] is True
    assert result["conflict_type"] == "power_overload"
    assert result["resolved"] is False
    assert result["final_action"] is None
    assert result["selected_source_node"] is None


def test_aggregate_overload_selects_highest_ranked_safe_single_decision():
    result = resolve_decisions(
        consistency_request(
            energy_decision("edge_1", 60, 0.7),
            energy_decision("edge_2", 50, 0.9),
        )
    )

    assert result["resolved"] is True
    assert result["selected_source_node"] == "edge_2"


def test_power_equal_to_limit_is_safe():
    result = resolve_decisions(consistency_request(energy_decision("edge_1", 100, 0.9)))

    assert result["resolved"] is True
    assert result["final_action"] == "charge"


def test_edge_scheduler_post_defaults_to_network_sim_proxy(monkeypatch):
    monkeypatch.setattr(
        edge_app,
        "config",
        {
            "services": {"scheduler": {"host": "10.9.8.7", "port": 9123}},
            "mqtt": {},
            "bearing_window_transfer": {},
        },
    )
    monkeypatch.delenv("SCHEDULER_SERVICE_BASE_URL", raising=False)

    assembly = edge_app._build_runtime()

    # 网络模拟模式：出站默认经过 Toxiproxy 代理端口，不再读 local.yaml 直连地址。
    assert assembly.service.config.scheduler.base_url == "http://127.0.0.1:18011"


def test_edge_scheduler_post_uses_env_override(monkeypatch):
    monkeypatch.setattr(
        edge_app,
        "config",
        {"services": {"scheduler": {"host": "127.0.0.1", "port": 8003}}, "mqtt": {}, "bearing_window_transfer": {}},
    )
    monkeypatch.setenv("SCHEDULER_SERVICE_BASE_URL", "http://10.9.8.7:9123")

    assembly = edge_app._build_runtime()

    assert assembly.service.config.scheduler.base_url == "http://10.9.8.7:9123"


def test_default_config_includes_consistency_service():
    assert DEFAULT_CONFIG["services"]["consistency"] == {"host": "127.0.0.1", "port": 8005}
