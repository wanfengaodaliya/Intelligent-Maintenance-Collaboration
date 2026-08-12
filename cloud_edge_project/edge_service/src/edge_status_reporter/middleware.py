# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .state import EdgeApplicationState


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class EdgeActivityMiddleware:
    TRACKED_PATHS = frozenset({"/edge/infer", "/edge/tasks", "/edge/packets"})

    def __init__(self, app: Any, *, state: EdgeApplicationState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        tracked = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in self.TRACKED_PATHS
        )
        if not tracked:
            await self.app(scope, receive, send)
            return
        self.state.touch_task_activity()
        try:
            await self.app(scope, receive, send)
        finally:
            self.state.touch_task_activity()
