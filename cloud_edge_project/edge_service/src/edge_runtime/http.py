# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from common.control_auth import ControlAuthError, ControlAuthVerifier
from edge_task_ingress import TASK_CONFLICT, EdgeTaskIngress

from .json_utils import json_bytes


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


def make_control_server(
    host: str, port: int, application: EdgeControlApplication
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
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
            except ValueError:
                status, result = 400, _error("INVALID_REQUEST", "invalid request body")
            body = json_bytes(result)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


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
