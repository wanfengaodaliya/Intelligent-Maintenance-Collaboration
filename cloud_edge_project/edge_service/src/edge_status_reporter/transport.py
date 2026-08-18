# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

import requests

from .config import StatusTargetConfig


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

    def send(self, payload: Mapping[str, Any]) -> bool:
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
                self._warn(attempt, total_attempts, type(exc).__name__)
                if attempt < total_attempts:
                    continue
                return False
            except Exception as exc:
                self._warn(attempt, total_attempts, type(exc).__name__)
                return False
            if 200 <= status_code < 300:
                return True
            self._warn(attempt, total_attempts, f"HTTP_{status_code}")
            if status_code < 500 or attempt >= total_attempts:
                return False
        return False

    def _warn(self, attempt: int, total_attempts: int, error: str) -> None:
        self.logger.warning(
            "状态上报失败 target=%s attempt=%d/%d error=%s",
            self.name,
            attempt,
            total_attempts,
            error,
        )
