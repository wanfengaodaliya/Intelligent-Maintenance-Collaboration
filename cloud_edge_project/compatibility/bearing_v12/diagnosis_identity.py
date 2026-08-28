"""Canonical identities for the legacy bearing V1.2 contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize an identity exactly as specified by the V1.2 shared contract."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_id(prefix: str, identity: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:24]
    return prefix + digest


def build_diagnosis_window_id(
    *,
    device_id: str,
    task_id: str,
    bearing_id: str,
    sender_id: str,
    window_start_sequence: int,
    window_end_sequence: int,
) -> str:
    identity = DiagnosisIdentity(
        device_id=device_id,
        task_id=task_id,
        bearing_id=bearing_id,
        sender_id=sender_id,
        window_start_sequence=window_start_sequence,
        window_end_sequence=window_end_sequence,
    )
    return _stable_id("dw_", identity.as_window_mapping())


def build_decision_round_id(
    *,
    device_id: str,
    task_id: str,
    window_start_sequence: int,
    window_end_sequence: int,
) -> str:
    _validate_text("device_id", device_id)
    _validate_text("task_id", task_id)
    _validate_sequence_range(window_start_sequence, window_end_sequence)
    return _stable_id(
        "round_",
        {
            "device_id": device_id,
            "task_id": task_id,
            "window_start_sequence": window_start_sequence,
            "window_end_sequence": window_end_sequence,
        },
    )


def build_run_id(*, device_id: str, batch_created_timestamp_ns: int) -> str:
    """Build the shared identity for sender tasks started in the same batch."""

    _validate_text("device_id", device_id)
    if (
        isinstance(batch_created_timestamp_ns, bool)
        or not isinstance(batch_created_timestamp_ns, int)
        or batch_created_timestamp_ns <= 0
    ):
        raise ValueError("batch_created_timestamp_ns must be a positive integer")
    return _stable_id(
        "run_",
        {
            "device_id": device_id,
            "batch_created_timestamp_ns": batch_created_timestamp_ns,
        },
    )


def build_summary_window_id(
    *,
    device_id: str,
    run_id: str | None,
    window_start_sequence: int,
    window_end_sequence: int,
) -> str:
    """Build the cross-service identity for one shared summary window."""

    _validate_text("device_id", device_id)
    if run_id is not None:
        _validate_text("run_id", run_id)
    _validate_sequence_range(window_start_sequence, window_end_sequence)
    return _stable_id(
        "summary_window_",
        {
            "device_id": device_id,
            "run_id": run_id or "",
            "window_start_sequence": window_start_sequence,
            "window_end_sequence": window_end_sequence,
        },
    )


@dataclass(frozen=True)
class DiagnosisIdentity:
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    window_start_sequence: int
    window_end_sequence: int

    def __post_init__(self) -> None:
        for field in ("device_id", "task_id", "bearing_id", "sender_id"):
            _validate_text(field, getattr(self, field))
        _validate_sequence_range(self.window_start_sequence, self.window_end_sequence)

    def as_window_mapping(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "task_id": self.task_id,
            "bearing_id": self.bearing_id,
            "sender_id": self.sender_id,
            "window_start_sequence": self.window_start_sequence,
            "window_end_sequence": self.window_end_sequence,
        }

    @property
    def diagnosis_window_id(self) -> str:
        return _stable_id("dw_", self.as_window_mapping())

    @property
    def decision_round_id(self) -> str:
        return build_decision_round_id(
            device_id=self.device_id,
            task_id=self.task_id,
            window_start_sequence=self.window_start_sequence,
            window_end_sequence=self.window_end_sequence,
        )


def _validate_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_sequence_range(start: object, end: object) -> None:
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
    ):
        raise ValueError("window sequence range must contain positive ordered integers")
