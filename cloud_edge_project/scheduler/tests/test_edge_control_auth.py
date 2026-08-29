from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from common.control_auth import ControlAuthVerifier
from scheduler import assignment_scheduler
from scheduler.assignment_scheduler import AssignmentError
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


def test_assignment_dispatch_carries_run_id_to_edge(monkeypatch) -> None:
    """validate→request_assignment 必须把 run_id 原样写入发给 Edge 的控制请求体。"""
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
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
        "run_id": "run_batch01",
        "expected_packet_count": 80,
        "created_timestamp_ns": 1,
    }

    EdgeAssignmentClient(shared_secret=SECRET).request_assignment(node, request)

    sent = json.loads(captured["request"].data.decode("utf-8"))
    assert sent["run_id"] == "run_batch01"


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


def _http_error(code: int, body: dict) -> HTTPError:
    return HTTPError(
        "http://edge.example/edge/tasks",
        code,
        "Error",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def _request() -> dict:
    return {
        "device_id": "device_01",
        "sender_id": "sender_1",
        "task_id": "sd_1_tk_0001",
        "bearing_id": "bearing_1",
        "expected_packet_count": 80,
        "created_timestamp_ns": 1,
    }


def _dispatch(monkeypatch, exc: HTTPError):
    monkeypatch.setattr(
        assignment_scheduler,
        "urlopen",
        lambda _request, *, timeout: (_ for _ in ()).throw(exc),
    )
    node = SimpleNamespace(
        config=SimpleNamespace(
            edge_node_id="edge_01", control_url="http://edge.example"
        )
    )
    return EdgeAssignmentClient(shared_secret=SECRET).request_assignment(node, _request())


def test_401_response_preserves_real_http_error_not_ack_mismatch(monkeypatch) -> None:
    # Edge 因控制密钥不一致返回 401，响应体不是 ack 对象。绝不能误报成
    # "ack task_id does not match"，而应保留真实 401 与错误信息。
    with pytest.raises(AssignmentError) as excinfo:
        _dispatch(monkeypatch, _http_error(401, {"error_code": "AUTH_FAILED"}))
    err = excinfo.value
    assert err.code == "EDGE_ACK_FAILED"
    assert err.status_code == 401
    assert "task_id does not match" not in err.message
    assert "401" in err.message
    assert "AUTH_FAILED" in err.message


def test_403_response_preserves_real_http_error(monkeypatch) -> None:
    with pytest.raises(AssignmentError) as excinfo:
        _dispatch(monkeypatch, _http_error(403, {"error_code": "FORBIDDEN"}))
    err = excinfo.value
    assert err.code == "EDGE_ACK_FAILED"
    assert err.status_code == 403
    assert "FORBIDDEN" in err.message


def test_non_2xx_with_accepted_ack_is_not_reported_as_task_mismatch(monkeypatch) -> None:
    # 非 2xx 但响应体声称 ACCEPTED 属异常，仍应保留 HTTP 状态而非 ack 校验错误。
    body = {
        "task_id": "sd_1_tk_0001",
        "edge_node_id": "edge_01",
        "ack_status": "ACCEPTED",
        "reason_code": None,
        "received_at_ns": 1,
        "acknowledged_at_ns": 2,
    }
    with pytest.raises(AssignmentError) as excinfo:
        _dispatch(monkeypatch, _http_error(500, body))
    err = excinfo.value
    assert err.code == "EDGE_ACK_FAILED"
    assert err.status_code == 500
    assert "task_id does not match" not in err.message


def test_real_rejected_ack_on_non_2xx_still_returns_validated_ack(monkeypatch) -> None:
    # Edge 明确返回 REJECTED ack（可能带非 2xx 状态）仍按仲裁拒绝处理。
    body = {
        "task_id": "sd_1_tk_0001",
        "edge_node_id": "edge_01",
        "ack_status": "REJECTED",
        "reason_code": "busy",
        "received_at_ns": 1,
        "acknowledged_at_ns": 2,
    }
    ack = _dispatch(monkeypatch, _http_error(409, body))
    assert ack["ack_status"] == "REJECTED"
    assert ack["reason_code"] == "busy"
