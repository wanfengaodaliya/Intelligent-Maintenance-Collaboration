"""Bounded retry reporting for edge packet-routing decisions."""
# 该模块负责将边缘单包路由结果以有限重试方式上报给调度器。

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from .http import HttpRequestError


class PacketRouteReporter:
    def __init__(
        self,
        post: Callable[[str, Mapping[str, Any]], dict[str, Any]],
        *,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.post = post
        self.wait = wait

    def report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        delays = (0.05, 0.10)
        for attempt in range(3):
            try:
                return self.post("/scheduler/packet-route", payload)
            except HttpRequestError as error:
                if not error.retryable or attempt == 2:
                    raise
                self.wait(delays[attempt])
        raise AssertionError("unreachable")


class DeviceArbitrationReporter:
    """Bounded retry sender for a V1.2 device conflict route request."""

    def __init__(
        self,
        post: Callable[[str, Mapping[str, Any]], dict[str, Any]],
        *,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.post = post
        self.wait = wait

    def report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        delays = (0.05, 0.10)
        for attempt in range(3):
            try:
                return self.post("/scheduler/device-arbitration-route", payload)
            except HttpRequestError as error:
                if not error.retryable or attempt == 2:
                    raise
                self.wait(delays[attempt])
        raise AssertionError("unreachable")
