# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from .config import StatusTargetConfig


@dataclass(frozen=True)
class SendOutcome:
    """结构化发送结果，供 Reporter 聚合统计与 /health 暴露。

    error 只保存短标签（异常类型/HTTP 码），不携带完整 traceback，
    上限很短，避免把内部细节刷进运维可见状态。
    """

    success: bool
    status_code: int | None = None
    error: str | None = None
    attempts: int = 0


class HttpStatusTarget:
    def __init__(
        self,
        config: StatusTargetConfig,
        *,
        http_post: Callable[..., Any] = requests.post,
        logger: logging.Logger | None = None,
    ) -> None:
        self.name = config.name
        self.url = config.url
        self.timeout_seconds = config.timeout_seconds
        self.retry_count = config.retry_count
        self.http_post = http_post
        self.logger = logger or logging.getLogger(__name__)

    def send(self, payload: Mapping[str, Any]) -> SendOutcome:
        """发送并返回结构化结果；重试失败的逐次日志降为 DEBUG。

        EDGE-1：WARNING 聚合与恢复日志交给 Reporter 的 DeliveryTracker，
        Transport 内不再刷屏。
        """
        total_attempts = self.retry_count + 1
        for attempt in range(1, total_attempts + 1):
            try:
                response = self.http_post(
                    self.url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                status_code = int(response.status_code)
            except (requests.RequestException, OSError, TimeoutError) as exc:
                self._debug(attempt, total_attempts, type(exc).__name__)
                if attempt < total_attempts:
                    continue
                return SendOutcome(
                    success=False,
                    status_code=None,
                    error=type(exc).__name__,
                    attempts=attempt,
                )
            except Exception as exc:
                self._debug(attempt, total_attempts, type(exc).__name__)
                return SendOutcome(
                    success=False,
                    status_code=None,
                    error=type(exc).__name__,
                    attempts=attempt,
                )
            if 200 <= status_code < 300:
                return SendOutcome(
                    success=True,
                    status_code=status_code,
                    attempts=attempt,
                )
            self._debug(attempt, total_attempts, f"HTTP_{status_code}")
            if status_code < 500 or attempt >= total_attempts:
                return SendOutcome(
                    success=False,
                    status_code=status_code,
                    error=f"HTTP_{status_code}",
                    attempts=attempt,
                )
        return SendOutcome(
            success=False,
            error="UNKNOWN",
            attempts=total_attempts,
        )

    def _debug(self, attempt: int, total_attempts: int, error: str) -> None:
        self.logger.debug(
            "状态上报重试失败 target=%s attempt=%d/%d error=%s",
            self.name,
            attempt,
            total_attempts,
            error,
        )
