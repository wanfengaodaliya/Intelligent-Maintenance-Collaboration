"""Thread-safe UTF-8 JSONL writer with bounded file rotation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any

from pydantic import BaseModel, SecretStr


ErrorHandler = Callable[[str, str], None]
_SENSITIVE_KEY_MARKERS = ("authorization", "token", "password", "secret")
_BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)[^\s,;]+")
_AUTHORIZATION_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"([\"']?[A-Za-z0-9_-]*authorization[A-Za-z0-9_-]*"
    r"[\"']?\s*[:=]\s*[\"']?)"
    r"((?:Bearer|Basic|Digest|Token|ApiKey)\s+)?"
    r"[^\"'\\\s,;}]+"
    r"([\"']?)"
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"([\"']?[A-Za-z0-9_-]*(?:token|password|secret)[A-Za-z0-9_-]*"
    r"[\"']?\s*[:=]\s*[\"']?)"
    r"[^\"'\\\s,;}]+"
    r"([\"']?)"
)


def sanitize_for_log(value: Any) -> Any:
    """Convert supported values to JSON-safe data and redact credentials."""

    if isinstance(value, SecretStr):
        return "***"
    if isinstance(value, BaseModel):
        return sanitize_for_log(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_for_log(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): (
                "***"
                if any(marker in str(key).lower() for marker in _SENSITIVE_KEY_MARKERS)
                else sanitize_for_log(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("log datetime must be timezone-aware")
        return value.isoformat()
    if isinstance(value, str):
        redacted = _AUTHORIZATION_ASSIGNMENT_PATTERN.sub(r"\1\2***\3", value)
        redacted = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(r"\1***\2", redacted)
        return _BEARER_PATTERN.sub(r"\1***", redacted)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonlWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int,
        backup_count: int,
        error_handler: ErrorHandler | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least one")
        if backup_count < 0:
            raise ValueError("backup_count cannot be negative")
        self.path = Path(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._error_handler = error_handler
        self._lock = Lock()

    def write(self, event: Mapping[str, Any]) -> bool:
        try:
            payload = sanitize_for_log(event)
            line = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ) + "\n"
            encoded_size = len(line.encode("utf-8"))
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self._should_rotate(encoded_size):
                    self._rotate()
                with self.path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(line)
            return True
        except Exception as exc:
            if self._error_handler is not None:
                try:
                    self._error_handler(str(self.path), type(exc).__name__)
                except Exception:
                    pass
            return False

    def _should_rotate(self, incoming_size: int) -> bool:
        if not self.path.exists():
            return False
        return self.path.stat().st_size + incoming_size > self._max_bytes

    def _rotate(self) -> None:
        if self._backup_count == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
