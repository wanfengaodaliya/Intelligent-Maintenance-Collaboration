from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .aggregation import build_incomplete_window_result, build_window_result
from .contracts import EXPECTED_BEARING_IDS, group_key, normalize_bearing_result
from .repository import SummaryRepository


class SummaryService:
    def __init__(
        self,
        repository: SummaryRepository,
        *,
        publish_window_result: Callable[[Mapping[str, Any]], None] | None = None,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.repository = repository
        self.publish_window_result = publish_window_result
        self.now_ns = now_ns

    def ingest(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        result = normalize_bearing_result(payload)
        if result["bearing_id"] not in EXPECTED_BEARING_IDS:
            raise ValueError(f"unsupported bearing_id: {result['bearing_id']}")

        inserted = self.repository.save_bearing_result(
            result, received_at_ns=self.now_ns()
        )
        device_id, window_start, window_end = group_key(result)
        existing = self.repository.get_window_result(device_id, window_start, window_end)
        if existing is not None:
            return existing
        if not inserted:
            return None

        source_results = self.repository.load_window_bearing_results(
            device_id, window_start, window_end
        )
        if len(source_results) < len(EXPECTED_BEARING_IDS):
            return None

        window_result = build_window_result(
            source_results, closed_at_ns=self.now_ns()
        )
        created = self.repository.save_window_result(window_result)
        if created and self.publish_window_result is not None:
            self.publish_window_result(window_result)
        return window_result

    def close_expired(self, *, now_ns: int, timeout_ns: int) -> int:
        if timeout_ns <= 0:
            raise ValueError("timeout_ns must be positive")
        closed = 0
        for source_results in self.repository.load_expired_open_windows(
            cutoff_ns=int(now_ns) - int(timeout_ns)
        ):
            if len(source_results) >= len(EXPECTED_BEARING_IDS):
                window_result = build_window_result(
                    source_results, closed_at_ns=int(now_ns)
                )
            else:
                window_result = build_incomplete_window_result(
                    source_results, closed_at_ns=int(now_ns)
                )
            if self.repository.save_window_result(window_result):
                closed += 1
                if self.publish_window_result is not None:
                    self.publish_window_result(window_result)
        return closed
