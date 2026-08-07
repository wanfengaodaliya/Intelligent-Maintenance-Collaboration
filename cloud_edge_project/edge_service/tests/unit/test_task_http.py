# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path


_CLOUD_ROOT = Path(__file__).resolve().parents[3]
if str(_CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(_CLOUD_ROOT))

from edge_service.app import app, register_edge_task  # noqa: E402


def _dispatch(task_id: str, **overrides) -> dict:
    result = {
        "task_id": task_id,
        "target_edge_node_id": "edge_1",
        "task_type": "BEARING_EDGE_INFERENCE",
        "input_ref": {
            "device_id": "device-1",
            "expected_bearing_ids": ["bearing-1"],
            "assigned_bearings": [
                {
                    "bearing_id": "bearing-1",
                    "sender_id": "sender-1",
                    "expected_packet_count": 80,
                }
            ],
        },
        "dispatched_at_ns": 1_700_000_000_000_000_000,
    }
    result.update(overrides)
    return result


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_task_route_is_exposed_and_returns_json_ack():
    assert any(
        route.path == "/edge/tasks" and "POST" in route.methods
        for route in app.routes
    )

    response = register_edge_task(_dispatch("task-http-accepted"))
    body = _body(response)

    assert response.status_code == 200
    assert response.media_type == "application/json"
    assert body == {
        "task_id": "task-http-accepted",
        "edge_node_id": "edge_1",
        "ack_status": "ACCEPTED",
        "reason_code": None,
        "received_at_ns": body["received_at_ns"],
        "acknowledged_at_ns": body["acknowledged_at_ns"],
    }
    assert body["acknowledged_at_ns"] >= body["received_at_ns"]


def test_identical_dispatch_is_idempotent_and_conflict_returns_409():
    dispatch = _dispatch("task-http-idempotent")
    first = register_edge_task(dispatch)
    duplicate = register_edge_task(dispatch)
    conflict = _dispatch("task-http-idempotent")
    conflict["input_ref"]["assigned_bearings"][0]["sender_id"] = "sender-changed"
    rejected = register_edge_task(conflict)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert _body(duplicate) == _body(first)
    assert rejected.status_code == 409
    assert _body(rejected)["reason_code"] == "TASK_CONFLICT"


def test_invalid_and_wrong_target_dispatches_return_json_rejections():
    invalid = _dispatch("task-http-invalid")
    del invalid["dispatched_at_ns"]
    wrong_target = _dispatch("task-http-wrong-target", target_edge_node_id="edge-2")

    invalid_response = register_edge_task(invalid)
    target_response = register_edge_task(wrong_target)

    assert invalid_response.status_code == 400
    assert _body(invalid_response)["reason_code"] == "INVALID_TASK"
    assert target_response.status_code == 400
    assert _body(target_response)["reason_code"] == "TARGET_NODE_MISMATCH"
