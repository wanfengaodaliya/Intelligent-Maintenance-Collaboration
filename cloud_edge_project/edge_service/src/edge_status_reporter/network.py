"""Read the Edge-to-Scheduler link applied by the network simulator."""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping

import requests

from .contracts import NetworkSnapshot


class SimulationNetworkCollector:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 0.5,
        stale_after_seconds: float = 3.0,
        http_get: Callable[..., Any] = requests.get,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.http_get = http_get
        self.clock_ns = clock_ns
        self.monotonic = monotonic
        self._last_snapshot: NetworkSnapshot | None = None
        self._last_observed_at: float | None = None

    def collect(self) -> NetworkSnapshot:
        try:
            response = self.http_get(self.url, timeout=self.timeout_seconds)
            if not 200 <= int(response.status_code) < 300:
                raise ValueError("network simulator returned HTTP %s" % response.status_code)
            snapshot = self._from_payload(response.json())
        except Exception:
            if (
                self._last_snapshot is not None
                and self._last_observed_at is not None
                and self.monotonic() - self._last_observed_at <= self.stale_after_seconds
            ):
                return self._last_snapshot
            return NetworkSnapshot(self.clock_ns(), 0.0, 0.0, 0.0, 1.0)
        self._last_snapshot = snapshot
        self._last_observed_at = self.monotonic()
        return snapshot

    def _from_payload(self, payload: Any) -> NetworkSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("network simulator response must be an object")
        available = payload.get("available") is True and payload.get("last_apply_success") is True
        parameters = payload.get("applied_parameters")
        if not available or not isinstance(parameters, Mapping):
            return NetworkSnapshot(self._measured_at(payload), 0.0, 0.0, 0.0, 1.0)
        latency = _non_negative(parameters.get("latency_ms"))
        jitter = _non_negative(parameters.get("jitter_ms"))
        bandwidth_kbps = _non_negative(parameters.get("bandwidth_kbps"))
        packet_loss_percent = min(_non_negative(parameters.get("packet_loss_percent")), 100.0)
        return NetworkSnapshot(
            self._measured_at(payload),
            bandwidth_kbps / 1000.0,
            latency,
            latency + 2.0 * jitter,
            packet_loss_percent / 100.0,
        )

    def _measured_at(self, payload: Mapping[str, Any]) -> int:
        value = payload.get("last_apply_timestamp_ns")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return self.clock_ns()


def _non_negative(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("network metric must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("network metric must be finite and non-negative")
    return result
