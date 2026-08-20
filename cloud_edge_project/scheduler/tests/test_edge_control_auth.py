from __future__ import annotations

import json
from types import SimpleNamespace

from common.control_auth import ControlAuthVerifier
from scheduler import assignment_scheduler
from scheduler.assignment_scheduler import EdgeAssignmentClient
from scheduler import deferred_device_dispatcher
from scheduler.deferred_device_dispatcher import EdgeArbitrationResultClient


SECRET = b"test-control-secret-that-is-at-least-32-bytes"


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_assignment_client_signs_the_exact_body(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            {
                "task_id": "sd_1_tk_0001",
                "edge_node_id": "edge_01",
                "ack_status": "ACCEPTED",
                "reason_code": None,
                "received_at_ns": 1,
                "acknowledged_at_ns": 2,
            }
        )

    monkeypatch.setattr(assignment_scheduler, "urlopen", fake_urlopen)
    node = SimpleNamespace(
        config=SimpleNamespace(
            edge_node_id="edge_01", control_url="http://edge.example"
        )
    )
    request = {
        "device_id": "device_01",
        "sender_id": "sender_1",
        "task_id": "sd_1_tk_0001",
        "bearing_id": "bearing_1",
        "expected_packet_count": 80,
        "created_timestamp_ns": 1,
    }

    EdgeAssignmentClient(shared_secret=SECRET).request_assignment(node, request)

    sent = captured["request"]
    ControlAuthVerifier(SECRET).verify(
        method="POST",
        path="/edge/tasks",
        query_string="",
        body=sent.data,
        headers=dict(sent.header_items()),
    )


def test_arbitration_result_client_signs_the_exact_body(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"accepted": True}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(deferred_device_dispatcher.requests, "post", fake_post)

    result = EdgeArbitrationResultClient(shared_secret=SECRET).deliver(
        "http://edge.example", {"result_id": "result-1"}
    )

    assert result == {"accepted": True}
    ControlAuthVerifier(SECRET).verify(
        method="POST",
        path="/edge/device-arbitration-results",
        query_string="",
        body=captured["data"],
        headers=captured["headers"],
    )
