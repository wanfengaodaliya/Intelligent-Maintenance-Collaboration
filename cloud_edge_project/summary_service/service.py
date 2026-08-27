from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .aggregation import build_incomplete_window_result, build_window_result
from .contracts import EXPECTED_BEARING_IDS, group_key, normalize_bearing_result
from .repository import SummaryRepository
from .suggestions import build_final_suggestion


class SummaryService:
    def __init__(
        self,
        repository: SummaryRepository,
        *,
        publish_window_result: Callable[[Mapping[str, Any]], None] | None = None,
        build_suggestion: Callable[[Mapping[str, Any]], Mapping[str, Any]] = build_final_suggestion,
        now_ns: Callable[[], int] = time.time_ns,
        expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
    ) -> None:
        expected = tuple(expected_bearing_ids)
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        unsupported = sorted(set(expected) - set(EXPECTED_BEARING_IDS))
        if unsupported:
            raise ValueError(f"unsupported expected bearing IDs: {', '.join(unsupported)}")
        self.repository = repository
        self.publish_window_result = publish_window_result
        self.build_suggestion = build_suggestion
        self.now_ns = now_ns
        self.expected_bearing_ids = expected

    def ingest(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        result = normalize_bearing_result(payload)
        if result["bearing_id"] not in self.expected_bearing_ids:
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
        if len(source_results) < len(self.expected_bearing_ids):
            return None

        window_result = build_window_result(
            source_results,
            closed_at_ns=self.now_ns(),
            expected_bearing_ids=self.expected_bearing_ids,
        )
        suggestion = (
            self.build_suggestion(window_result)
            if window_result["result_status"] == "FINAL"
            else None
        )
        created = self.repository.save_window_result(
            window_result, suggestion=suggestion
        )
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
            if len(source_results) >= len(self.expected_bearing_ids):
                window_result = build_window_result(
                    source_results,
                    closed_at_ns=int(now_ns),
                    expected_bearing_ids=self.expected_bearing_ids,
                )
            else:
                window_result = build_incomplete_window_result(
                    source_results,
                    closed_at_ns=int(now_ns),
                    expected_bearing_ids=self.expected_bearing_ids,
                )
            suggestion = (
                self.build_suggestion(window_result)
                if window_result["result_status"] == "FINAL"
                else None
            )
            if self.repository.save_window_result(
                window_result, suggestion=suggestion
            ):
                closed += 1
                if self.publish_window_result is not None:
                    self.publish_window_result(window_result)
        return closed
