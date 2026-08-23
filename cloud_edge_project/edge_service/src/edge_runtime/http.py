# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from common.control_auth import ControlAuthError, ControlAuthVerifier
from edge_task_ingress import TASK_CONFLICT, EdgeTaskIngress

from .json_utils import json_bytes


CONTROL_MAX_BODY_BYTES = 64 * 1024
CONTROL_READ_TIMEOUT_SECONDS = 5.0
CONTROL_REJECT_DRAIN_TIMEOUT_SECONDS = 0.1
CONTROL_MAX_WORKERS = 16
CONTROL_WAITING_CAPACITY = 32


class HttpRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class JsonHttpClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request(path, method="POST", payload=payload)

    def get(self, path: str) -> dict[str, Any]:
        return self._request(path, method="GET")

    def _request(
        self,
        path: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json_bytes(dict(payload)) if payload is not None else None,
            headers={"Content-Type": "application/json"} if payload is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HttpRequestError(
                "HTTP_%d" % exc.code,
                detail or str(exc),
                retryable=exc.code >= 500,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HttpRequestError("HTTP_UNAVAILABLE", str(exc), retryable=True) from exc
        if not body:
            return {}
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HttpRequestError("INVALID_HTTP_RESPONSE", str(exc), retryable=False) from exc
        if not isinstance(result, dict):
            raise HttpRequestError(
                "INVALID_HTTP_RESPONSE", "response must be a JSON object", retryable=False
            )
        return result


class SchedulerReporter:
    def __init__(
        self,
        client: JsonHttpClient,
        *,
        status_path: str,
    ):
        self.client = client
        self.status_path = status_path

    def report_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.client.post(self.status_path, payload)


class EdgeControlApplication:
    def __init__(
        self,
        ingress: EdgeTaskIngress,
        *,
        on_device_arbitration_result: Callable[[dict[str, Any]], Any] | None = None,
        control_auth_verifier: ControlAuthVerifier | None = None,
    ):
        self.ingress = ingress
        self.on_device_arbitration_result = on_device_arbitration_result
        self.control_auth_verifier = control_auth_verifier

    def handle(
        self,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        method: str = "POST",
        query_string: str = "",
        raw_body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self.control_auth_verifier is not None:
            try:
                self.control_auth_verifier.verify(
                    method=method,
                    path=path,
                    query_string=query_string,
                    body=(
                        json_bytes({} if payload is None else dict(payload))
                        if raw_body is None
                        else raw_body
                    ),
                    headers={} if headers is None else headers,
                )
            except ControlAuthError as exc:
                return exc.status_code, _error(exc.code, exc.message)
        if payload is None:
            return 400, _error("INVALID_REQUEST", "invalid request body")
        if path == "/edge/tasks":
            ack = self.ingress.register_task(payload)
            if ack.ack_status == "ACCEPTED":
                status = 200
            elif ack.reason_code == TASK_CONFLICT:
                status = 409
            else:
                status = 400
            return status, ack.as_dict()
        if path == "/edge/task-revocations":
            dispatch_id = payload.get("dispatch_id")
            reason = payload.get("reason_code")
            revoked_at_ns = payload.get("revoked_at_ns")
            try:
                found = self.ingress.revoke_dispatch(
                    dispatch_id,
                    reason_code=reason,
                    revoked_at_ns=revoked_at_ns,
                )
            except (TypeError, ValueError):
                return 400, _error("INVALID_REQUEST", "invalid revocation request")
            if not found:
                return 404, _error("DISPATCH_NOT_FOUND", "dispatch_id is unknown")
            return 200, {"dispatch_id": dispatch_id, "revoked": True}
        if path == "/edge/device-arbitration-results":
            if self.on_device_arbitration_result is None:
                return 404, _error("NOT_FOUND", "device arbitration is not enabled")
            try:
                result = self.on_device_arbitration_result(dict(payload))
            except (TypeError, ValueError):
                return 400, _error(
                    "INVALID_DEVICE_ARBITRATION_RESULT",
                    "invalid device arbitration result",
                )
            return 200, {
                "accepted": True,
                "device_result_id": None if result is None else result.result_id,
            }
        return 404, _error("NOT_FOUND", "unknown edge control endpoint")


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-pool HTTP server with a fixed active + waiting request budget."""

    request_queue_size = CONTROL_WAITING_CAPACITY

    def __init__(
        self,
        server_address,
        request_handler_class,
        *,
        max_workers: int = CONTROL_MAX_WORKERS,
        waiting_capacity: int = CONTROL_WAITING_CAPACITY,
    ) -> None:
        if max_workers <= 0 or waiting_capacity < 0:
            raise ValueError("control server concurrency limits are invalid")
        super().__init__(server_address, request_handler_class)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="edge-control-request",
        )
        self._request_slots = threading.BoundedSemaphore(
            max_workers + waiting_capacity
        )
        self._inflight_lock = threading.Lock()
        self._inflight_requests = 0

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        with self._inflight_lock:
            self._inflight_requests += 1
        try:
            future = self._executor.submit(
                self.process_request_thread, request, client_address
            )
        except Exception:
            self._request_finished()
            self.shutdown_request(request)
            raise
        future.add_done_callback(self._request_finished_callback)

    def _request_finished_callback(self, _future: Future) -> None:
        self._request_finished()

    def _request_finished(self) -> None:
        with self._inflight_lock:
            self._inflight_requests -= 1
        self._request_slots.release()

    @property
    def inflight_request_count(self) -> int:
        with self._inflight_lock:
            return self._inflight_requests

    def _reject_busy(self, request) -> None:
        body = json_bytes(_error("CONTROL_BUSY", "control service is busy"))
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        try:
            request.sendall(response)
            request.shutdown(socket.SHUT_WR)
            request.settimeout(CONTROL_REJECT_DRAIN_TIMEOUT_SECONDS)
            remaining = CONTROL_MAX_BODY_BYTES
            while remaining > 0:
                chunk = request.recv(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        self._executor.shutdown(wait=True)


def make_control_server(
    host: str,
    port: int,
    application: EdgeControlApplication,
    *,
    max_body_bytes: int = CONTROL_MAX_BODY_BYTES,
    read_timeout_seconds: float = CONTROL_READ_TIMEOUT_SECONDS,
    max_workers: int = CONTROL_MAX_WORKERS,
    waiting_capacity: int = CONTROL_WAITING_CAPACITY,
) -> BoundedThreadingHTTPServer:
    if max_body_bytes <= 0 or read_timeout_seconds <= 0:
        raise ValueError("control server request limits must be positive")

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(read_timeout_seconds)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    raise ValueError("invalid Content-Length")
                if length > max_body_bytes:
                    self._send_json(
                        413,
                        _error(
                            "CONTROL_BODY_TOO_LARGE",
                            "control request body exceeds the configured limit",
                        ),
                    )
                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                        deadline = (
                            time.monotonic()
                            + CONTROL_REJECT_DRAIN_TIMEOUT_SECONDS
                        )
                        remaining = length
                        while remaining > 0:
                            timeout = deadline - time.monotonic()
                            if timeout <= 0:
                                break
                            self.connection.settimeout(timeout)
                            chunk = self.rfile.read(min(8192, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                    except OSError:
                        pass
                    return
                raw_body = self.rfile.read(length)
                target = urlsplit(self.path)
                try:
                    decoded = json.loads(raw_body.decode("utf-8"))
                    payload = decoded if isinstance(decoded, dict) else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                status, result = application.handle(
                    target.path,
                    payload,
                    method="POST",
                    query_string=target.query,
                    raw_body=raw_body,
                    headers=self.headers,
                )
            except socket.timeout:
                status, result = 408, _error(
                    "CONTROL_READ_TIMEOUT", "control request body timed out"
                )
            except ValueError:
                status, result = 400, _error("INVALID_REQUEST", "invalid request body")
            self._send_json(status, result)

        def _send_json(self, status: int, result: Mapping[str, Any]) -> None:
            body = json_bytes(dict(result))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return BoundedThreadingHTTPServer(
        (host, port),
        Handler,
        max_workers=max_workers,
        waiting_capacity=waiting_capacity,
    )


class HeartbeatLoop:
    def __init__(self, interval_seconds: float, callback: Callable[[], None]):
        self.interval_seconds = interval_seconds
        self.callback = callback
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="edge-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.callback()
            except Exception:
                pass
            self._stop.wait(self.interval_seconds)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}
