"""ASGI request-body limits enforced before application parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        default_limit_bytes: int,
        path_limits: Mapping[str, int] | None = None,
    ) -> None:
        if default_limit_bytes <= 0:
            raise ValueError("default_limit_bytes must be positive")
        selected = dict(path_limits or {})
        if any(limit <= 0 for limit in selected.values()):
            raise ValueError("path body limits must be positive")
        self.app = app
        self.default_limit_bytes = default_limit_bytes
        self.path_limits = selected

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self.path_limits.get(
            scope.get("path", ""), self.default_limit_bytes
        )
        declared = _content_length(scope.get("headers", ()))
        if declared is not None and declared < 0:
            await _send_error(
                send, 400, "INVALID_CONTENT_LENGTH", "invalid Content-Length"
            )
            return
        if declared is not None and declared > limit:
            await _send_too_large(send)
            return

        messages: list[dict[str, Any]] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            received += len(message.get("body", b""))
            if received > limit:
                await _send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index >= len(messages):
                return {"type": "http.disconnect"}
            message = messages[index]
            index += 1
            return message

        await self.app(scope, replay_receive, send)


def _content_length(headers) -> int | None:
    for name, value in headers:
        if name.lower() != b"content-length":
            continue
        try:
            return int(value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return -1
    return None


async def _send_too_large(send) -> None:
    await _send_error(
        send,
        413,
        "REQUEST_BODY_TOO_LARGE",
        "request body exceeds the configured limit",
    )


async def _send_error(send, status: int, code: str, message: str) -> None:
    body = json.dumps(
        {"error_code": code, "message": message},
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
