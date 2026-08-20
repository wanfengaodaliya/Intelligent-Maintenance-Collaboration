"""Read the Edge-to-Scheduler link applied by the network simulator."""

from __future__ import annotations

import math
import time
from dataclasses import replace
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
        self._last_ok_measured_at_ns: int | None = None

    def collect(self) -> NetworkSnapshot:
        try:
            response = self.http_get(self.url, timeout=self.timeout_seconds)
            if not 200 <= int(response.status_code) < 300:
                raise ValueError("network simulator returned HTTP %s" % response.status_code)
            snapshot = self._from_payload(response.json())
        except Exception:
            cached = self._cached_snapshot()
            if cached is not None:
                # 采集失败但缓存仍在有效期内：沿用缓存数值与原始测量时间，
                # 仅将状态降级为 STALE，不伪装成新一轮成功测量。
                return replace(
                    cached,
                    measurement_status="STALE",
                    last_successful_measurement_ns=self._last_ok_measured_at_ns,
                )
            return NetworkSnapshot(
                self.clock_ns(),
                0.0,
                0.0,
                0.0,
                1.0,
                measurement_status="FAILED",
                last_successful_measurement_ns=self._last_ok_measured_at_ns,
            )
        self._last_snapshot = snapshot
        self._last_observed_at = self.monotonic()
        if snapshot.measurement_status == "OK":
            self._last_ok_measured_at_ns = snapshot.measured_at_ns
        return snapshot

    def _cached_snapshot(self) -> NetworkSnapshot | None:
        if (
            self._last_snapshot is not None
            and self._last_observed_at is not None
            and self.monotonic() - self._last_observed_at <= self.stale_after_seconds
        ):
            return self._last_snapshot
        return None

    def _from_payload(self, payload: Any) -> NetworkSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("network simulator response must be an object")
        available = payload.get("available") is True
        apply_success = payload.get("last_apply_success") is True
        if not available:
            # 模拟器明确声明链路断开：零值表示断链，而不是“极优网络”。
            return NetworkSnapshot(
                self._measured_at(payload),
                0.0,
                0.0,
                0.0,
                1.0,
                measurement_status="DISCONNECTED",
                last_successful_measurement_ns=self._last_ok_measured_at_ns,
            )
        parameters = payload.get("applied_parameters")
        if not apply_success or not isinstance(parameters, Mapping):
            # 链路未断开，但 Toxiproxy 参数未成功施加：
            # 当前数值不可信，不能冒充正常测量结果。
            return NetworkSnapshot(
                self._measured_at(payload),
                0.0,
                0.0,
                0.0,
                1.0,
                measurement_status="FAILED",
                last_successful_measurement_ns=self._last_ok_measured_at_ns,
            )
        latency = _non_negative(parameters.get("latency_ms"))
        jitter = _non_negative(parameters.get("jitter_ms"))
        bandwidth_kbps = _non_negative(parameters.get("bandwidth_kbps"))
        packet_loss_percent = min(_non_negative(parameters.get("packet_loss_percent")), 100.0)
        measured_at_ns = self._measured_at(payload)
        return NetworkSnapshot(
            measured_at_ns,
            bandwidth_kbps / 1000.0,
            latency,
            latency + 2.0 * jitter,
            packet_loss_percent / 100.0,
            measurement_status="OK",
            last_successful_measurement_ns=measured_at_ns,
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
