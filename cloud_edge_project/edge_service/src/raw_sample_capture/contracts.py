"""Contracts for the asynchronous P1 raw-waveform evidence chain."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Mapping


RAW_ANALYSIS_SAMPLE_SCHEMA_VERSION = "raw-analysis-sample/1.0"


@dataclass(frozen=True)
class CaptureDecision:
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    decision_round_id: str
    trigger_reasons: tuple[str, ...]
    sample_type: str
    requested_window_ms: int
    edge_model_version: str
    cloud_corrected: bool | None
    created_at_ns: int


@dataclass(frozen=True)
class RawAnalysisSample:
    sample_id: str
    device_id: str
    task_id: str
    bearing_id: str
    sender_id: str
    decision_round_id: str
    trigger_reasons: tuple[str, ...]
    sample_type: str
    requested_window_ms: int
    actual_window_ms: int
    complete: bool
    missing_duration_ms: int
    window_start_ns: int
    window_end_ns: int
    packet_manifest: tuple[Mapping[str, Any], ...]
    sample_rate_hz: int
    sample_count: int
    payload_sha256: str
    edge_model_version: str
    cloud_corrected: bool | None
    created_at_ns: int
    payload: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RAW_ANALYSIS_SAMPLE_SCHEMA_VERSION,
            "sample_id": self.sample_id,
            "device_id": self.device_id,
            "task_id": self.task_id,
            "bearing_id": self.bearing_id,
            "sender_id": self.sender_id,
            "decision_round_id": self.decision_round_id,
            "trigger_reasons": list(self.trigger_reasons),
            "sample_type": self.sample_type,
            "requested_window_ms": self.requested_window_ms,
            "actual_window_ms": self.actual_window_ms,
            "complete": self.complete,
            "missing_duration_ms": self.missing_duration_ms,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "packet_manifest": [dict(item) for item in self.packet_manifest],
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": self.sample_count,
            "payload_sha256": self.payload_sha256,
            "edge_model_version": self.edge_model_version,
            "cloud_corrected": self.cloud_corrected,
            "created_at_ns": self.created_at_ns,
        }

    def with_payload(self, payload: bytes) -> "RawAnalysisSample":
        return replace(self, payload=payload, payload_sha256=hashlib.sha256(payload).hexdigest())


@dataclass(frozen=True)
class InsertOutcome:
    status: str
    sample_id: str


@dataclass(frozen=True)
class QueuedRawSample:
    sample: RawAnalysisSample
    status: str
    attempt_count: int
    next_attempt_at_ns: int | None
    last_error: str | None
