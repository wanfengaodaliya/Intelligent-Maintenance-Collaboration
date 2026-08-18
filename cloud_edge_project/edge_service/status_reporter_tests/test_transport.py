from __future__ import annotations

from types import SimpleNamespace

from edge_status_reporter.config import StatusTargetConfig
from edge_status_reporter.transport import HttpStatusTarget


def _config(retry_count: int = 1) -> StatusTargetConfig:
    return StatusTargetConfig(
        name="scheduler",
        enabled=True,
        url="http://127.0.0.1:8003/scheduler/edge-nodes/status",
        timeout_seconds=0.5,
        retry_count=retry_count,
    )


def test_target_retries_server_error_then_succeeds() -> None:
    responses = iter((SimpleNamespace(status_code=503), SimpleNamespace(status_code=200)))
    calls: list[dict] = []

    def post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return next(responses)

    target = HttpStatusTarget(_config(), http_post=post)
    payload = {"reported_at_ns": 1}

    assert target.send(payload) is True
    assert len(calls) == 2
    assert calls[0]["json"] is payload


def test_target_does_not_retry_client_error() -> None:
    calls = 0

    def post(url, *, json, timeout):
        nonlocal calls
        calls += 1
        return SimpleNamespace(status_code=400)

    target = HttpStatusTarget(_config(retry_count=3), http_post=post)

    assert target.send({"reported_at_ns": 1}) is False
    assert calls == 1


def test_target_retries_network_error_without_raising() -> None:
    calls = 0

    def post(url, *, json, timeout):
        nonlocal calls
        calls += 1
        raise OSError("offline")

    target = HttpStatusTarget(_config(retry_count=2), http_post=post)

    assert target.send({"reported_at_ns": 1}) is False
    assert calls == 3
