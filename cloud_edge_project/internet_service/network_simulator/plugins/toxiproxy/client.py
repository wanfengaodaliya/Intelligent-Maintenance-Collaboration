"""HTTP adapter for idempotent Toxiproxy v2.12.0 operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, ROUND_HALF_UP
import json as json_module
import re
from threading import Condition, Lock, local
import time
from typing import Any
from urllib.parse import quote

import requests

from domain.enums import DisconnectMode
from domain.exceptions import (
    ProxyConflictError,
    ToxicOperationError,
    ToxiproxyUnavailableError,
)


OperationLogger = Callable[[Mapping[str, Any]], None]
_AUTHORIZATION_LINE = re.compile(r"(?im)(authorization\s*:\s*)[^\r\n]+")
_QUOTED_SENSITIVE_VALUE = re.compile(
    r"(?i)([\"']?(?:token|password|secret)[\"']?\s*[:=]\s*)([\"'])(.*?)\2"
)
_PLAIN_SENSITIVE_VALUE = re.compile(
    r"(?i)(\b(?:token|password|secret)\b\s*[:=]\s*)[^,;\r\n]+"
)
_MAX_ERROR_BODY_CHARS = 512


def kbps_to_kbytes_per_second(bandwidth_kbps: int) -> int:
    """Convert Kbps to Toxiproxy's integer KB/s using strict half-up rounding."""

    if bandwidth_kbps <= 0:
        raise ValueError("bandwidth_kbps must be greater than zero")
    converted = (Decimal(bandwidth_kbps) / Decimal(8)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return max(1, int(converted))


class _RequestError(ToxiproxyUnavailableError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ToxiproxyClient:
    DYNAMIC_LATENCY = "dynamic_latency"
    DYNAMIC_BANDWIDTH = "dynamic_bandwidth"
    DYNAMIC_DISCONNECT = "dynamic_disconnect"
    DYNAMIC_PACKET_LOSS = "dynamic_packet_loss"

    def __init__(
        self,
        api_base_url: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        retry_count: int,
        backoff_base_seconds: float,
        *,
        session: requests.Session | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        operation_logger: OperationLogger | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("Toxiproxy timeouts must be greater than zero")
        if retry_count < 0:
            raise ValueError("retry_count cannot be negative")
        if backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds cannot be negative")
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = (connect_timeout_seconds, read_timeout_seconds)
        self._retry_count = retry_count
        self._backoff_base_seconds = backoff_base_seconds
        self._provided_session = session
        self._provided_session_lock = Lock()
        self._session_factory = session_factory
        self._thread_local = local()
        self._sessions_lock = Lock()
        self._sessions: list[requests.Session] = []
        self._lifecycle_condition = Condition()
        self._active_requests = 0
        self._closing = False
        self._closed = False
        if session is not None:
            self._sessions.append(session)
        self._operation_logger = operation_logger
        self._sleep = sleep

    def close(self) -> None:
        with self._lifecycle_condition:
            if self._closed:
                return
            if self._closing:
                while not self._closed:
                    self._lifecycle_condition.wait()
                return
            self._closing = True
            while self._active_requests:
                self._lifecycle_condition.wait()
        try:
            with self._sessions_lock:
                sessions = tuple(self._sessions)
                self._sessions.clear()
            for session in sessions:
                session.close()
        finally:
            with self._lifecycle_condition:
                self._closed = True
                self._closing = False
                self._lifecycle_condition.notify_all()

    def health_check(
        self,
        *,
        timeout_seconds: float | None = None,
        attempts: int | None = None,
    ) -> bool:
        timeout = (
            None
            if timeout_seconds is None
            else self._bounded_timeout(timeout_seconds)
        )
        try:
            self._request(
                "GET",
                "/version",
                expected_statuses={200},
                attempts=attempts,
                timeout=timeout,
            )
        except ToxiproxyUnavailableError:
            return False
        return True

    def get_proxy(self, name: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"/proxies/{quote(name, safe='')}",
            expected_statuses={200, 404},
        )
        if response.status_code == 404:
            return None
        return self._json_object(response, f"proxy {name}")

    def ensure_proxy(self, name: str, listen: str, upstream: str) -> None:
        expected = {"name": name, "listen": listen, "upstream": upstream}
        existing = self.get_proxy(name)
        if existing is None:
            self._create_proxy_idempotently({**expected, "enabled": True})
            return
        self._validate_proxy(existing, expected)
        if existing.get("enabled") is not True:
            self._request(
                "POST",
                f"/proxies/{quote(name, safe='')}",
                expected_statuses={200},
                json={"enabled": True},
            )

    def delete_proxy(self, name: str) -> None:
        self._request(
            "DELETE",
            f"/proxies/{quote(name, safe='')}",
            expected_statuses={200, 204, 404},
        )

    def upsert_latency(
        self,
        proxy_name: str,
        latency_ms: int,
        jitter_ms: int,
        stream: str,
    ) -> None:
        if latency_ms < 0 or jitter_ms < 0:
            raise ValueError("latency_ms and jitter_ms cannot be negative")
        self._upsert_toxic(
            proxy_name,
            self.DYNAMIC_LATENCY,
            "latency",
            stream,
            {"latency": latency_ms, "jitter": jitter_ms},
        )

    def upsert_bandwidth(
        self,
        proxy_name: str,
        bandwidth_kbps: int,
        stream: str,
    ) -> None:
        self._upsert_toxic(
            proxy_name,
            self.DYNAMIC_BANDWIDTH,
            "bandwidth",
            stream,
            {"rate": kbps_to_kbytes_per_second(bandwidth_kbps)},
        )

    def set_disconnected(
        self,
        proxy_name: str,
        mode: DisconnectMode | str,
        stream: str,
    ) -> None:
        try:
            resolved_mode = DisconnectMode(mode)
        except ValueError as exc:
            raise ValueError("disconnect mode must be timeout or reset_peer") from exc
        if resolved_mode is DisconnectMode.NONE:
            raise ValueError("disconnect mode must be timeout or reset_peer")
        self._upsert_toxic(
            proxy_name,
            self.DYNAMIC_DISCONNECT,
            resolved_mode.value,
            stream,
            {"timeout": 0},
        )

    def clear_disconnect(self, proxy_name: str) -> None:
        self.delete_toxic(proxy_name, self.DYNAMIC_DISCONNECT)

    def clear_dynamic_toxics(self, proxy_name: str) -> None:
        for toxic_name in (
            self.DYNAMIC_LATENCY,
            self.DYNAMIC_BANDWIDTH,
            self.DYNAMIC_DISCONNECT,
            self.DYNAMIC_PACKET_LOSS,
        ):
            self.delete_toxic(proxy_name, toxic_name)

    def delete_toxic(self, proxy_name: str, toxic_name: str) -> None:
        path = self._toxic_path(proxy_name, toxic_name)
        try:
            response = self._request(
                "DELETE", path, expected_statuses={200, 204, 404}
            )
        except ToxiproxyUnavailableError as exc:
            self._log_operation(
                proxy_name, toxic_name, "delete", None, {}, False, None, str(exc)
            )
            raise ToxicOperationError(
                f"failed to delete toxic {proxy_name}/{toxic_name}"
            ) from exc
        self._log_operation(
            proxy_name,
            toxic_name,
            "delete",
            None,
            {},
            True,
            response.status_code,
            None,
        )

    def _create_proxy_idempotently(self, payload: Mapping[str, Any]) -> None:
        name = str(payload["name"])
        last_error: _RequestError | None = None
        for attempt in range(self._retry_count + 1):
            try:
                self._request(
                    "POST",
                    "/proxies",
                    expected_statuses={200, 201},
                    json=payload,
                    attempts=1,
                )
                return
            except _RequestError as exc:
                last_error = exc
            try:
                recovered = self._get_proxy_once(name)
            except _RequestError:
                recovered = None
            if recovered is not None:
                self._validate_proxy(recovered, payload)
                if recovered.get("enabled") is not True:
                    self._request(
                        "POST",
                        f"/proxies/{quote(name, safe='')}",
                        expected_statuses={200},
                        json={"enabled": True},
                    )
                return
            if not getattr(last_error, "retryable", False):
                raise last_error
            if attempt < self._retry_count:
                self._backoff(attempt)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validate_proxy(
        existing: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> None:
        if (
            existing.get("name") != expected["name"]
            or not ToxiproxyClient._listen_matches(
                existing.get("listen"), expected["listen"]
            )
            or existing.get("upstream") != expected["upstream"]
        ):
            raise ProxyConflictError(
                f"proxy {expected['name']} conflicts with configured listen/upstream"
            )

    @staticmethod
    def _listen_matches(actual: Any, expected: Any) -> bool:
        if actual == expected:
            return True
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        actual_host, actual_separator, actual_port = actual.rpartition(":")
        expected_host, expected_separator, expected_port = expected.rpartition(":")
        if not actual_separator or not expected_separator or actual_port != expected_port:
            return False
        wildcard_hosts = {"0.0.0.0", "::"}
        return (
            actual_host.strip("[]") in wildcard_hosts
            and expected_host.strip("[]") in wildcard_hosts
        )

    def _upsert_toxic(
        self,
        proxy_name: str,
        toxic_name: str,
        toxic_type: str,
        stream: str,
        attributes: Mapping[str, Any],
    ) -> None:
        if stream not in {"upstream", "downstream"}:
            raise ValueError("stream must be upstream or downstream")
        payload = {
            "name": toxic_name,
            "type": toxic_type,
            "stream": stream,
            "toxicity": 1.0,
            "attributes": dict(attributes),
        }
        operation = "update"
        try:
            existing = self._get_toxic(proxy_name, toxic_name)
            if existing is None:
                operation = "create"
                response, operation = self._create_toxic_idempotently(
                    proxy_name, toxic_name, payload
                )
            elif existing.get("type") not in {None, toxic_type}:
                # Toxiproxy PATCH preserves the existing toxic type, so a type
                # change is a replacement: delete first, then use create POST.
                self.delete_toxic(proxy_name, toxic_name)
                response, operation = self._create_toxic_idempotently(
                    proxy_name, toxic_name, payload
                )
            else:
                response = self._request(
                    "PATCH",
                    self._toxic_path(proxy_name, toxic_name),
                    expected_statuses={200},
                    json=payload,
                )
        except (ToxiproxyUnavailableError, ToxicOperationError) as exc:
            self._log_operation(
                proxy_name,
                toxic_name,
                operation,
                stream,
                attributes,
                False,
                None,
                str(exc),
            )
            raise ToxicOperationError(
                f"failed to {operation} toxic {proxy_name}/{toxic_name}"
            ) from exc
        self._log_operation(
            proxy_name,
            toxic_name,
            operation,
            stream,
            attributes,
            True,
            response.status_code,
            None,
        )

    def _get_toxic(
        self, proxy_name: str, toxic_name: str
    ) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            self._toxic_path(proxy_name, toxic_name),
            expected_statuses={200, 404},
        )
        if response.status_code == 404:
            return None
        return self._json_object(response, f"toxic {proxy_name}/{toxic_name}")

    def _get_proxy_once(self, name: str) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            f"/proxies/{quote(name, safe='')}",
            expected_statuses={200, 404},
            attempts=1,
        )
        if response.status_code == 404:
            return None
        return self._json_object(response, f"proxy {name}")

    def _get_toxic_once(
        self, proxy_name: str, toxic_name: str
    ) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            self._toxic_path(proxy_name, toxic_name),
            expected_statuses={200, 404},
            attempts=1,
        )
        if response.status_code == 404:
            return None
        return self._json_object(response, f"toxic {proxy_name}/{toxic_name}")

    def _create_toxic_idempotently(
        self,
        proxy_name: str,
        toxic_name: str,
        payload: Mapping[str, Any],
    ) -> tuple[requests.Response, str]:
        proxy = quote(proxy_name, safe="")
        last_error: _RequestError | None = None
        for attempt in range(self._retry_count + 1):
            try:
                response = self._request(
                    "POST",
                    f"/proxies/{proxy}/toxics",
                    expected_statuses={200, 201},
                    json=payload,
                    attempts=1,
                )
                return response, "create"
            except _RequestError as exc:
                last_error = exc
            try:
                recovered = self._get_toxic_once(proxy_name, toxic_name)
            except _RequestError:
                recovered = None
            if recovered is not None:
                response = self._request(
                    "PATCH",
                    self._toxic_path(proxy_name, toxic_name),
                    expected_statuses={200},
                    json=payload,
                )
                return response, "update"
            if not getattr(last_error, "retryable", False):
                raise last_error
            if attempt < self._retry_count:
                self._backoff(attempt)
        assert last_error is not None
        raise last_error

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json: Mapping[str, Any] | None = None,
        attempts: int | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> requests.Response:
        with self._lifecycle_condition:
            if self._closing or self._closed:
                raise ToxiproxyUnavailableError("Toxiproxy client is closed")
            self._active_requests += 1
        try:
            return self._request_active(
                method,
                path,
                expected_statuses=expected_statuses,
                json=json,
                attempts=attempts,
                timeout=timeout,
            )
        finally:
            with self._lifecycle_condition:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._lifecycle_condition.notify_all()

    def _request_active(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json: Mapping[str, Any] | None = None,
        attempts: int | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> requests.Response:
        maximum_attempts = self._retry_count + 1 if attempts is None else attempts
        if maximum_attempts < 1:
            raise ValueError("attempts must be at least one")
        request_timeout = self._timeout if timeout is None else timeout
        last_error: _RequestError | None = None
        for attempt in range(maximum_attempts):
            try:
                session = self._session_for_current_thread()
                if self._provided_session is None:
                    response = session.request(
                        method,
                        f"{self._api_base_url}{path}",
                        json=json,
                        timeout=request_timeout,
                    )
                else:
                    with self._provided_session_lock:
                        response = session.request(
                            method,
                            f"{self._api_base_url}{path}",
                            json=json,
                            timeout=request_timeout,
                        )
            except requests.RequestException as exc:
                last_error = _RequestError(
                    f"{method} {path} failed ({type(exc).__name__})",
                    retryable=True,
                )
            else:
                if response.status_code in expected_statuses:
                    return response
                excerpt = self._safe_response_excerpt(response)
                suffix = f"; response={excerpt}" if excerpt else ""
                last_error = _RequestError(
                    f"{method} {path} returned HTTP {response.status_code}{suffix}",
                    status_code=response.status_code,
                    retryable=response.status_code >= 500,
                )
            if not last_error.retryable or attempt + 1 == maximum_attempts:
                break
            self._backoff(attempt)
        assert last_error is not None
        raise last_error

    def _bounded_timeout(self, timeout_seconds: float) -> tuple[float, float]:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        configured_total = self._timeout[0] + self._timeout[1]
        scale = min(1.0, timeout_seconds / configured_total)
        return self._timeout[0] * scale, self._timeout[1] * scale

    def _backoff(self, zero_based_attempt: int) -> None:
        self._sleep(self._backoff_base_seconds * (2**zero_based_attempt))

    @staticmethod
    def _json_object(
        response: requests.Response, description: str
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise ToxiproxyUnavailableError(
                f"invalid JSON returned for {description}"
            ) from exc
        if not isinstance(payload, dict):
            raise ToxiproxyUnavailableError(
                f"JSON returned for {description} must be an object"
            )
        return payload

    @staticmethod
    def _safe_response_excerpt(response: requests.Response) -> str:
        text = response.text
        try:
            payload = json_module.loads(text)
        except (ValueError, TypeError):
            sanitized = _AUTHORIZATION_LINE.sub(r"\1***", text)
            sanitized = _QUOTED_SENSITIVE_VALUE.sub(r"\1\2***\2", sanitized)
            sanitized = _PLAIN_SENSITIVE_VALUE.sub(r"\1***", sanitized)
        else:
            sanitized = json_module.dumps(
                ToxiproxyClient._redact_json(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return sanitized[:_MAX_ERROR_BODY_CHARS]

    @staticmethod
    def _redact_json(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "***"
                    if any(
                        marker in str(key).lower()
                        for marker in ("authorization", "token", "password", "secret")
                    )
                    else ToxiproxyClient._redact_json(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [ToxiproxyClient._redact_json(item) for item in value]
        return value

    def _session_for_current_thread(self) -> requests.Session:
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._session_factory()
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    @staticmethod
    def _toxic_path(proxy_name: str, toxic_name: str) -> str:
        return (
            f"/proxies/{quote(proxy_name, safe='')}/toxics/"
            f"{quote(toxic_name, safe='')}"
        )

    def _log_operation(
        self,
        proxy_name: str,
        toxic_name: str,
        operation: str,
        stream: str | None,
        attributes: Mapping[str, Any],
        success: bool,
        status_code: int | None,
        error: str | None,
    ) -> None:
        if self._operation_logger is None:
            return
        event = {
            "proxy_name": proxy_name,
            "toxic_name": toxic_name,
            "operation": operation,
            "stream": stream,
            "attributes": dict(attributes),
            "success": success,
            "status_code": status_code,
            "error": error,
        }
        try:
            self._operation_logger(event)
        except Exception:
            return
