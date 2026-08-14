"""Deterministic raw-sample trigger policy."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import CaptureDecision


class RawSampleCapturePolicy:
    def __init__(
        self,
        *,
        history_window_ms: int = 1_000,
        normal_sample_interval_seconds: int = 60,
        low_confidence_threshold: float = 0.8,
    ) -> None:
        if history_window_ms <= 0 or normal_sample_interval_seconds <= 0:
            raise ValueError("raw sample capture intervals must be positive")
        if not 0.0 <= low_confidence_threshold <= 1.0:
            raise ValueError("low_confidence_threshold must be in [0, 1]")
        self.history_window_ms = history_window_ms
        self.normal_sample_interval_ns = normal_sample_interval_seconds * 1_000_000_000
        self.low_confidence_threshold = low_confidence_threshold
        self._last_normal_capture_ns: dict[tuple[str, str, str, str], int] = {}

    def evaluate(self, event: Mapping[str, Any]) -> CaptureDecision | None:
        identity = _identity(event)
        created_at_ns = _positive_int(event.get("created_at_ns"), "created_at_ns")
        reasons: list[str] = []
        confidence = event.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < self.low_confidence_threshold:
            reasons.append("LOW_CONFIDENCE")
        if event.get("route") in {"CLOUD_NOW", "DEFER"}:
            reasons.append("CLOUD_ROUTE_REQUESTED")
        if event.get("bearing_state") in {"abnormal", "fault"} or event.get("risk_level") in {"high", "critical"}:
            reasons.append("ABNORMAL_OR_HIGH_RISK")
        if event.get("device_conflict") is True:
            reasons.append("DEVICE_CONFLICT")
        sample_type = "event"
        if not reasons and event.get("bearing_state") == "normal":
            key = (identity["device_id"], identity["task_id"], identity["bearing_id"], identity["sender_id"])
            previous = self._last_normal_capture_ns.get(key)
            if previous is None or created_at_ns - previous >= self.normal_sample_interval_ns:
                reasons.append("PERIODIC_NORMAL_SAMPLE")
                sample_type = "periodic_normal"
                self._last_normal_capture_ns[key] = created_at_ns
        if event.get("explicit_sample") is True and "PERIODIC_NORMAL_SAMPLE" not in reasons:
            reasons.append("EXPLICIT_SAMPLE")
        if not reasons:
            return None
        return CaptureDecision(
            **identity,
            trigger_reasons=tuple(reasons),
            sample_type=sample_type,
            requested_window_ms=self.history_window_ms,
            edge_model_version=_text(event.get("edge_model_version"), "edge_model_version"),
            cloud_corrected=True if event.get("cloud_corrected") is True else None,
            created_at_ns=created_at_ns,
        )


def _identity(event: Mapping[str, Any]) -> dict[str, str]:
    return {field: _text(event.get(field), field) for field in (
        "device_id", "task_id", "bearing_id", "sender_id", "decision_round_id"
    )}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return value
