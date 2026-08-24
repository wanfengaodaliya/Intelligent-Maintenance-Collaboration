from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cloud_service.app as cloud_api
import pytest
from fastapi.testclient import TestClient
from cloud_service.device_arbitration.repository import DeviceArbitrationRepository
from cloud_service.global_analysis.result_repository import (
    GlobalAnalysisResultRepository,
)
from cloud_service.storage.database import connect, initialize_database
from cloud_service.task_results import TaskResultService


def _insert_device_decision(
    database_path: Path,
    *,
    device_id: str,
    result_id: str,
    received_at_ns: int,
    closed_at_ns: int | None = None,
) -> dict:
    record_time_ns = closed_at_ns if closed_at_ns is not None else received_at_ns - 1
    payload = {
        "result_id": result_id,
        "device_id": device_id,
        "task_id": f"task_{result_id}",
        "decision_round_id": f"round_{result_id}",
        "final_state": "normal",
        "has_conflict": False,
        "created_at_ns": record_time_ns,
        "closed_at_ns": record_time_ns,
    }
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO cloud_device_decision_result(
               result_id, device_id, task_id, decision_round_id, revision,
               replaces_result_id, payload_json, received_at_ns
               ) VALUES (?, ?, ?, ?, 1, NULL, ?, ?)""",
            (
                result_id,
                device_id,
                payload["task_id"],
                payload["decision_round_id"],
                json.dumps(payload),
                received_at_ns,
            ),
        )
    return payload


def _insert_global_analysis(
    database_path: Path, *, subject_id: str, analysis_id: str, created_at_ns: int
) -> dict:
    payload = {
        "analysis_id": analysis_id,
        "scenario_type": "bearing",
        "subject_id": subject_id,
        "analysis_window": {"actual_task_count": 20, "task_limit": 20},
        "device_health_analysis": {"latest_state": "normal", "trend": "stable"},
        "created_at_ns": created_at_ns,
    }
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
               analysis_id, scenario_type, subject_id, task_count, result_json,
               created_at_ns
               ) VALUES (?, 'bearing', ?, 20, ?, ?)""",
            (analysis_id, subject_id, json.dumps(payload), created_at_ns),
        )
    return payload


def test_recent_device_decisions_are_newest_first_and_filterable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cloud.db"
    service = TaskResultService(database_path)
    older = _insert_device_decision(
        database_path,
        device_id="machine_01",
        result_id="device_01",
        received_at_ns=20,
        closed_at_ns=10,
    )
    newer = _insert_device_decision(
        database_path,
        device_id="machine_02",
        result_id="device_02",
        received_at_ns=10,
        closed_at_ns=20,
    )

    assert service.list_recent_device_decisions(None, 10) == [newer, older]
    assert service.list_recent_device_decisions("machine_01", 10) == [older]


def test_recent_device_arbitrations_are_empty_when_no_conflicts_exist(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)

    assert DeviceArbitrationRepository(database_path).list_recent(None, 10) == []


def test_recent_global_analyses_are_newest_first_and_filterable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "cloud.db"
    repository = GlobalAnalysisResultRepository(database_path)
    older = _insert_global_analysis(
        database_path,
        subject_id="machine_01",
        analysis_id="analysis_01",
        created_at_ns=10,
    )
    newer = _insert_global_analysis(
        database_path,
        subject_id="machine_02",
        analysis_id="analysis_02",
        created_at_ns=20,
    )

    assert repository.list_recent("bearing", None, 10) == [newer, older]
    assert repository.list_recent("bearing", "machine_01", 10) == [older]


def test_recent_record_routes_return_uniform_envelopes(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "cloud.db"
    TaskResultService(database_path)
    decision = _insert_device_decision(
        database_path,
        device_id="machine_01",
        result_id="device_01",
        received_at_ns=10,
    )
    analysis = _insert_global_analysis(
        database_path,
        subject_id="machine_01",
        analysis_id="analysis_01",
        created_at_ns=10,
    )
    monkeypatch.setattr(
        cloud_api,
        "load_cloud_settings",
        lambda: SimpleNamespace(database_path=database_path),
    )

    assert cloud_api.list_recent_device_decisions("machine_01", 20) == {
        "success": True,
        "items": [decision],
        "count": 1,
    }
    assert cloud_api.list_recent_device_arbitrations(None, 20) == {
        "success": True,
        "items": [],
        "count": 0,
    }
    assert cloud_api.list_recent_global_analyses("bearing", None, 20) == {
        "success": True,
        "items": [analysis],
        "count": 1,
    }


def test_recent_record_routes_reject_out_of_range_limit() -> None:
    response = cloud_api.list_recent_global_analyses("bearing", None, 0)

    assert response.status_code == 400
    assert json.loads(response.body)["error_code"] == "INVALID_RECENT_LIMIT"


@pytest.mark.parametrize(
    "path",
    [
        "/cloud/device-decision-results/recent",
        "/cloud/device-arbitration/recent",
        "/cloud/global-analysis/recent",
    ],
)
@pytest.mark.parametrize("limit", ["abc", "0", "201"])
def test_recent_record_http_routes_return_stable_400_for_invalid_limits(
    path: str, limit: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        cloud_api,
        "load_cloud_settings",
        lambda: SimpleNamespace(database_path=tmp_path / "cloud.db"),
    )

    response = TestClient(cloud_api.app).get(path, params={"limit": limit})

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_RECENT_LIMIT"
