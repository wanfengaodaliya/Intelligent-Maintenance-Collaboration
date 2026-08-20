from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from edge_runtime.body_limit import RequestBodyLimitMiddleware
from edge_runtime.http import EdgeControlApplication, make_control_server


async def _asgi_request(
    middleware: RequestBodyLimitMiddleware,
    *,
    path: str,
    chunks: list[bytes],
    content_length: int | None = None,
):
    received_body = bytearray()
    called = False

    async def downstream(_scope, receive, send) -> None:
        nonlocal called
        called = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            received_body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware.app = downstream
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return messages.pop(0)

    sent = []

    async def send(message) -> None:
        sent.append(message)

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    await middleware(
        {"type": "http", "method": "POST", "path": path, "headers": headers},
        receive,
        send,
    )
    return called, bytes(received_body), sent


def test_asgi_limit_rejects_declared_and_streamed_oversize_bodies() -> None:
    middleware = RequestBodyLimitMiddleware(
        lambda *_args: None,
        default_limit_bytes=10,
        path_limits={"/edge/tasks": 4},
    )

    declared = asyncio.run(
        _asgi_request(
            middleware,
            path="/edge/data",
            chunks=[],
            content_length=11,
        )
    )
    streamed = asyncio.run(
        _asgi_request(
            middleware,
            path="/edge/tasks",
            chunks=[b"12", b"345"],
        )
    )

    for called, _body, sent in (declared, streamed):
        assert called is False
        assert sent[0]["status"] == 413
        assert json.loads(sent[1]["body"])["error_code"] == "REQUEST_BODY_TOO_LARGE"


def test_asgi_limit_replays_an_allowed_stream_exactly() -> None:
    middleware = RequestBodyLimitMiddleware(
        lambda *_args: None,
        default_limit_bytes=10,
    )

    called, body, sent = asyncio.run(
        _asgi_request(
            middleware,
            path="/edge/data",
            chunks=[b"123", b"456"],
        )
    )

    assert called is True
    assert body == b"123456"
    assert sent[0]["status"] == 204


@dataclass(frozen=True)
class _Ack:
    ack_status: str = "ACCEPTED"
    reason_code: str | None = None

    def as_dict(self) -> dict:
        return {"ack_status": self.ack_status}


class _Ingress:
    def register_task(self, _payload) -> _Ack:
        return _Ack()


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _post(port: int, body: bytes) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        connection.request(
            "POST",
            "/edge/tasks",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _raw_post(port: int, body: bytes) -> bytes:
    request = (
        b"POST /edge/tasks HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as client:
        client.sendall(request)
        return client.recv(4096)


def test_legacy_control_server_rejects_oversize_and_times_out_partial_reads() -> None:
    server = make_control_server(
        "127.0.0.1",
        0,
        EdgeControlApplication(_Ingress()),
        max_body_bytes=8,
        read_timeout_seconds=0.05,
    )
    thread = _serve(server)
    port = server.server_address[1]
    try:
        status, body = _post(port, b'{"too":"large"}')
        assert status == 413
        assert json.loads(body)["error"]["code"] == "CONTROL_BODY_TOO_LARGE"

        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as client:
            client.sendall(
                b"POST /edge/tasks HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 8\r\n\r\n"
                b"{}"
            )
            response = client.recv(4096)
        assert b" 408 " in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_legacy_control_server_bounds_active_and_waiting_requests() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingIngress:
        def register_task(self, _payload) -> _Ack:
            entered.set()
            release.wait(timeout=2.0)
            return _Ack()

    server = make_control_server(
        "127.0.0.1",
        0,
        EdgeControlApplication(BlockingIngress()),
        max_workers=1,
        waiting_capacity=1,
    )
    thread = _serve(server)
    port = server.server_address[1]
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first = executor.submit(_post, port, b"{}")
        assert entered.wait(timeout=1.0)
        second = executor.submit(_post, port, b"{}")
        deadline = time.monotonic() + 1.0
        while server.inflight_request_count < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server.inflight_request_count == 2

        response = _raw_post(port, b"{}")
        assert b" 503 " in response
        assert b"CONTROL_BUSY" in response

        release.set()
        assert first.result(timeout=2.0)[0] == 200
        assert second.result(timeout=2.0)[0] == 200
    finally:
        release.set()
        executor.shutdown(wait=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
