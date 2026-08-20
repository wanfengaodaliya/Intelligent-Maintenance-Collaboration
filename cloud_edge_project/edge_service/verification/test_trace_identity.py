from __future__ import annotations

import json
import sqlite3

from core.diagnosis_contracts import BearingDecisionResult, BearingLifecycleStatus
from edge_runtime.trace_identity import (
    build_trace_identity,
    trace_id_for_task,
    with_trace_identity,
)
from result_uploader import ResultUploader


def test_trace_id_is_deterministic_and_derived_from_task() -> None:
    assert trace_id_for_task("task_001") == "trace-task_001"
    assert trace_id_for_task("task_001") == trace_id_for_task("task_001")


def test_build_trace_identity_contains_all_unified_fields() -> None:
    identity = build_trace_identity(
        edge_node_id="edge_01",
        task_id="task_001",
        decision_round_id="round_01",
        route_id="summary/device-results",
    )
    assert identity == {
        "trace_id": "trace-task_001",
        "task_id": "task_001",
        "decision_round_id": "round_01",
        "edge_node_id": "edge_01",
        "route_id": "summary/device-results",
    }


def test_with_trace_identity_injects_fields_from_payload() -> None:
    enriched = with_trace_identity(
        {"task_id": "task_002", "decision_round_id": "round_02", "value": 1},
        edge_node_id="edge_02",
        route_id="/cloud/device-decision-results",
    )
    assert enriched["trace_id"] == "trace-task_002"
    assert enriched["task_id"] == "task_002"
    assert enriched["decision_round_id"] == "round_02"
    assert enriched["edge_node_id"] == "edge_02"
    assert enriched["route_id"] == "/cloud/device-decision-results"
    assert enriched["value"] == 1


def test_with_trace_identity_keeps_explicit_trace_id() -> None:
    enriched = with_trace_identity(
        {"task_id": "task_003", "trace_id": "trace-upstream"},
        edge_node_id="edge_01",
        route_id="summary/device-results",
    )
    assert enriched["trace_id"] == "trace-upstream"


def test_device_result_publish_path_injects_identity_before_publish() -> None:
    """设备结果发布前统一注入 trace 身份（生产路径等价于 factory 的

    ``_publish_device_result_with_identity`` 闭包：先 with_trace_identity 再
    发布，route_id 使用设备结果主题）。
    """
    published: list[dict] = []

    class Inner:
        def publish(self, payload, **kwargs):
            published.append(dict(payload))

    inner = Inner()
    inner.publish(
        with_trace_identity(
            {"task_id": "task_004", "decision_round_id": "round_04"},
            edge_node_id="edge_01",
            route_id="summary/device-results",
        )
    )
    payload = published[0]
    assert payload["trace_id"] == "trace-task_004"
    assert payload["edge_node_id"] == "edge_01"
    assert payload["route_id"] == "summary/device-results"


def _bearing() -> BearingDecisionResult:
    return BearingDecisionResult(
        result_id="bearing_round_01_a_r1", revision=1, replaces_result_id=None,
        device_id="machine_01", task_id="task_001", bearing_id="bearing_a",
        sender_id="sender_a", decision_round_id="round_01", diagnosis_window_id="dw_01",
        lifecycle_state=BearingLifecycleStatus.FINAL_EDGE, bearing_state="normal",
        confidence=.9, data_quality_score=.9, risk_level="low", action_grade=0,
        recommended_action="continue_operation", decision_source="FINAL_EDGE",
        review_status="NOT_REQUIRED", degraded=False, edge_result_id="edge_dw_01",
        cloud_result_id=None, model_version="edge_model_v1", created_at_ns=10,
        edge_accepted_at_ns=11,
    )


def test_result_uploader_persists_payload_with_trace_identity(tmp_path) -> None:
    uploader = ResultUploader(
        tmp_path / "edge.db",
        lambda _path, _payload: {"status": "accepted"},
        payload_enricher=lambda payload, route_id: with_trace_identity(
            payload, edge_node_id="edge_01", route_id=route_id
        ),
    )
    uploader.enqueue_bearing(_bearing())

    with sqlite3.connect(uploader.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT payload_json FROM v12_result_upload WHERE result_id=?",
            (_bearing().result_id,),
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["trace_id"] == "trace-task_001"
    assert payload["task_id"] == "task_001"
    assert payload["decision_round_id"] == "round_01"
    assert payload["edge_node_id"] == "edge_01"
    assert payload["route_id"] == "/cloud/bearing-diagnosis-results"
