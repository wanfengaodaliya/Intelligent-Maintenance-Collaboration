"""HMAC authentication for Scheduler-to-Edge control requests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any


CONTROL_PATHS = frozenset(
    {
        "/edge/tasks",
        "/edge/task-revocations",
        "/edge/device-arbitration-results",
    }
)
KEY_ID_HEADER = "X-Edge-Key-Id"
TIMESTAMP_HEADER = "X-Edge-Timestamp"
NONCE_HEADER = "X-Edge-Nonce"
SIGNATURE_HEADER = "X-Edge-Signature"
DEFAULT_KEY_ID = "scheduler-v1"
DEFAULT_MAX_SKEW_SECONDS = 60
DEFAULT_REPLAY_CACHE_CAPACITY = 10_000

_NONCE_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_TIMESTAMP_PATTERN = re.compile(r"^[0-9]{1,20}$")


class ControlAuthError(ValueError):
    """A stable, client-safe control authentication failure."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def load_control_shared_secret(
    environ: Mapping[str, str] | None = None,
) -> bytes:
    source = os.environ if environ is None else environ
    value = source.get("EDGE_CONTROL_SHARED_SECRET", "")
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise ValueError("EDGE_CONTROL_SHARED_SECRET must contain at least 32 bytes")
    return secret


def encode_control_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sign_control_request(
    secret: bytes | str,
    *,
    method: str,
    path: str,
    body: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
    key_id: str = DEFAULT_KEY_ID,
) -> dict[str, str]:
    if method != "POST" or path not in CONTROL_PATHS:
        raise ValueError("unsupported edge control target")
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce_value = secrets.token_hex(16) if nonce is None else nonce
    if not _NONCE_PATTERN.fullmatch(nonce_value):
        raise ValueError("control nonce must be 128-bit hexadecimal")
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    if len(secret_bytes) < 32:
        raise ValueError("control shared secret must contain at least 32 bytes")
    signature = hmac.new(
        secret_bytes,
        _signing_message(method, path, timestamp_text, nonce_value, body),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        KEY_ID_HEADER: key_id,
        TIMESTAMP_HEADER: timestamp_text,
        NONCE_HEADER: nonce_value,
        SIGNATURE_HEADER: signature,
    }


class ControlAuthVerifier:
    def __init__(
        self,
        secret: bytes | str,
        *,
        key_id: str = DEFAULT_KEY_ID,
        max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
        replay_cache_capacity: int = DEFAULT_REPLAY_CACHE_CAPACITY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < 32:
            raise ValueError("control shared secret must contain at least 32 bytes")
        if max_skew_seconds <= 0 or replay_cache_capacity <= 0:
            raise ValueError("control authentication limits must be positive")
        self._secret = secret_bytes
        self._key_id = key_id
        self._max_skew_seconds = max_skew_seconds
        self._replay_cache_capacity = replay_cache_capacity
        self._clock = clock
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "ControlAuthVerifier":
        return cls(
            load_control_shared_secret(),
            key_id=os.getenv("EDGE_CONTROL_KEY_ID") or DEFAULT_KEY_ID,
        )

    def verify(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None:
        if method != "POST" or path not in CONTROL_PATHS or query_string:
            raise ControlAuthError(
                "CONTROL_TARGET_INVALID", "invalid control request target", 401
            )

        normalized = {key.lower(): value for key, value in headers.items()}
        key_id = normalized.get(KEY_ID_HEADER.lower(), "")
        timestamp_text = normalized.get(TIMESTAMP_HEADER.lower(), "")
        nonce = normalized.get(NONCE_HEADER.lower(), "")
        signature = normalized.get(SIGNATURE_HEADER.lower(), "")
        if not all((key_id, timestamp_text, nonce, signature)):
            raise ControlAuthError(
                "CONTROL_AUTH_REQUIRED", "control authentication is required", 401
            )
        if key_id != self._key_id:
            raise ControlAuthError(
                "CONTROL_KEY_UNKNOWN", "control authentication failed", 401
            )
        if not _TIMESTAMP_PATTERN.fullmatch(timestamp_text):
            raise ControlAuthError(
                "CONTROL_TIMESTAMP_INVALID", "control authentication failed", 401
            )
        now = self._clock()
        timestamp = int(timestamp_text)
        if abs(now - timestamp) > self._max_skew_seconds:
            raise ControlAuthError(
                "CONTROL_TIMESTAMP_EXPIRED", "control request timestamp expired", 401
            )
        if not _NONCE_PATTERN.fullmatch(nonce) or not _SIGNATURE_PATTERN.fullmatch(
            signature
        ):
            raise ControlAuthError(
                "CONTROL_AUTH_INVALID", "control authentication failed", 401
            )

        expected = hmac.new(
            self._secret,
            _signing_message(method, path, timestamp_text, nonce, body),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise ControlAuthError(
                "CONTROL_AUTH_INVALID", "control authentication failed", 401
            )

        cache_key = f"{key_id}:{nonce.lower()}"
        with self._lock:
            self._prune_expired(now)
            if cache_key in self._nonces:
                raise ControlAuthError(
                    "CONTROL_REPLAY_DETECTED", "control request was already used", 409
                )
            if len(self._nonces) >= self._replay_cache_capacity:
                raise ControlAuthError(
                    "CONTROL_REPLAY_CACHE_FULL",
                    "control authentication is temporarily unavailable",
                    503,
                )
            # Keep the nonce until the signed timestamp itself is no longer
            # acceptable. A request dated near the future edge of the skew
            # window remains valid for almost twice the skew from first sight.
            self._nonces[cache_key] = timestamp + self._max_skew_seconds

    def _prune_expired(self, now: float) -> None:
        while self._nonces:
            _, expires_at = next(iter(self._nonces.items()))
            if expires_at >= now:
                return
            self._nonces.popitem(last=False)


def _signing_message(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join(
        ("v1", method, path, timestamp, nonce, body_hash)
    ).encode("utf-8")
