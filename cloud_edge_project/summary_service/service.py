from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .aggregation import build_incomplete_window_result, build_window_result
from .contracts import (
    EXPECTED_BEARING_IDS,
    EXPECTED_EDGE_NODE_IDS,
    normalize_bearing_result,
)
from .ports import BearingResultConflictError, SummaryStore


class SummaryService:
    def __init__(
        self,
        repository: SummaryStore,
        *,
        now_ns: Callable[[], int] = time.time_ns,
        expected_bearing_ids: Sequence[str] = EXPECTED_BEARING_IDS,
        expected_edge_node_ids: Sequence[str] = EXPECTED_EDGE_NODE_IDS,
    ) -> None:
        expected = tuple(expected_bearing_ids)
        if not expected or len(set(expected)) != len(expected):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        unsupported = sorted(set(expected) - set(EXPECTED_BEARING_IDS))
        if unsupported:
            raise ValueError(f"unsupported expected bearing IDs: {', '.join(unsupported)}")
        edges = tuple(expected_edge_node_ids)
        if not edges or len(set(edges)) != len(edges):
            raise ValueError("expected_edge_node_ids must be non-empty and unique")
        self.repository = repository
        self.now_ns = now_ns
        self.expected_bearing_ids = expected
        self.expected_edge_node_ids = edges

    def ingest(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Receive → validate → persist → aggregate → enqueue outbox work."""

        result = normalize_bearing_result(payload)
        if result["bearing_id"] not in self.expected_bearing_ids:
            raise ValueError(f"unsupported bearing_id: {result['bearing_id']}")
        if result["edge_node_id"] not in self.expected_edge_node_ids:
            self.repository.increment_metric("unknown_edge_node_results")
            raise ValueError(f"unsupported edge_node_id: {result['edge_node_id']}")

        summary_window_id = str(result["summary_window_id"])
        now = self.now_ns()
        try:
            inserted = self.repository.save_bearing_result(result, received_at_ns=now)
        except BearingResultConflictError as exc:
            if exc.reason != "edge_slot":
                # Masqueraded slots (same bearing / mutated result_id) stay poison.
                raise
            return self._freeze_same_edge_window(summary_window_id, now_ns=now)
        if not inserted:
            # Redelivery of a message we already persisted: never re-aggregate.
            self.repository.increment_metric("duplicate_bearing_result_messages")
            return self.repository.get_window_result(summary_window_id)

        existing = self.repository.get_window_result(summary_window_id)
        if existing is not None and existing["result_status"] != "INCOMPLETE":
            # FINAL / PENDING_ARBITRATION / MANUAL_REVIEW windows are settled;
            # late results must not reopen or re-arbitrate them.
            return existing

        source_results = self.repository.load_window_bearing_results(summary_window_id)
        if len(source_results) < len(self.expected_bearing_ids):
            # Still waiting for the full bearing set. An already-closed
            # INCOMPLETE window keeps its record until the set completes.
            return existing

        window_result = build_window_result(
            source_results,
            closed_at_ns=now,
            expected_bearing_ids=self.expected_bearing_ids,
            expected_edge_node_ids=self.expected_edge_node_ids,
        )
        self._record_close_duration(window_result, now)
        created = self.repository.save_window_result(window_result)
        if created is not None:
            return created
        return self.repository.get_window_result(summary_window_id) or window_result

    def close_expired(self, *, now_ns: int, timeout_ns: int) -> int:
        if timeout_ns <= 0:
            raise ValueError("timeout_ns must be positive")
        closed = 0
        for source_results in self.repository.load_expired_open_windows(
            cutoff_ns=int(now_ns) - int(timeout_ns)
        ):
            summary_window_id = str(source_results[0]["summary_window_id"])
            if self.repository.get_window_result(summary_window_id) is not None:
                continue
            if len(source_results) >= len(self.expected_bearing_ids):
                window_result = build_window_result(
                    source_results,
                    closed_at_ns=int(now_ns),
                    expected_bearing_ids=self.expected_bearing_ids,
                    expected_edge_node_ids=self.expected_edge_node_ids,
                )
            else:
                window_result = build_incomplete_window_result(
                    source_results,
                    closed_at_ns=int(now_ns),
                    expected_bearing_ids=self.expected_bearing_ids,
                )
            self._record_close_duration(window_result, int(now_ns))
            if self.repository.save_window_result(window_result) is not None:
                closed += 1
        return closed

    def _freeze_same_edge_window(
        self, summary_window_id: str, *, now_ns: int
    ) -> dict[str, Any] | None:
        """A node reports at most one bearing per window; a second submission
        from the same node can never reach edge diversity, so the window is
        closed as INCOMPLETE immediately instead of waiting for a timeout."""

        self.repository.increment_metric("same_edge_node_results")
        existing = self.repository.get_window_result(summary_window_id)
        if existing is not None:
            # Already closed or settled: the duplicate is just counted.
            return existing
        source_results = self.repository.load_window_bearing_results(summary_window_id)
        window_result = build_incomplete_window_result(
            source_results,
            closed_at_ns=int(now_ns),
            reason="INSUFFICIENT_EDGE_DIVERSITY",
            expected_bearing_ids=self.expected_bearing_ids,
        )
        self._record_close_duration(window_result, int(now_ns))
        return self.repository.save_window_result(window_result)

    def _record_close_duration(
        self, window_result: dict[str, Any], now_ns: int
    ) -> None:
        first_received = self.repository.first_received_at_ns(
            str(window_result["summary_window_id"])
        )
        if first_received is not None:
            window_result["window_close_duration_ns"] = int(now_ns) - int(first_received)
