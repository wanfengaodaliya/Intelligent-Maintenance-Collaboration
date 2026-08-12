from __future__ import annotations

import asyncio

import pytest

from edge_status_reporter.middleware import EdgeActivityMiddleware
from edge_status_reporter.state import EdgeApplicationState


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(_: dict) -> None:
    return None


def test_middleware_updates_only_tracked_business_paths() -> None:
    values = iter((11, 12))
    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="model",
        clock_ns=lambda: next(values),
    )
    calls: list[str] = []

    async def app(scope, receive, send) -> None:
        calls.append(scope["path"])

    middleware = EdgeActivityMiddleware(app, state=state)
    asyncio.run(middleware({"type": "http", "path": "/health"}, _receive, _send))
    assert state.snapshot().last_task_activity_ns == 0

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/edge/infer"},
            _receive,
            _send,
        )
    )
    assert state.snapshot().last_task_activity_ns == 12
    assert calls == ["/health", "/edge/infer"]


def test_middleware_preserves_application_exceptions_and_touches_finally() -> None:
    values = iter((21, 22))
    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="model",
        clock_ns=lambda: next(values),
    )

    async def app(scope, receive, send) -> None:
        raise RuntimeError("business failure")

    middleware = EdgeActivityMiddleware(app, state=state)
    with pytest.raises(RuntimeError, match="business failure"):
        asyncio.run(
            middleware(
                {"type": "http", "method": "POST", "path": "/edge/packets"},
                _receive,
                _send,
            )
        )
    assert state.snapshot().last_task_activity_ns == 22


def test_middleware_ignores_non_post_requests_on_business_paths() -> None:
    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="model",
        clock_ns=lambda: 99,
    )

    async def app(scope, receive, send) -> None:
        return None

    middleware = EdgeActivityMiddleware(app, state=state)
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": "/edge/infer"},
            _receive,
            _send,
        )
    )

    assert state.snapshot().last_task_activity_ns == 0
