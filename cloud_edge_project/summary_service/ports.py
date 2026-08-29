from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class BearingResultConflictError(ValueError):
    """A different bearing result already occupies this window slot."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SummaryStore(Protocol):
    """Storage operations used by the Summary business service."""

    def increment_metric(self, metric: str, amount: int = 1) -> None: ...

    def save_bearing_result(
        self, result: Mapping[str, Any], *, received_at_ns: int
    ) -> bool: ...

    def get_window_result(
        self, summary_window_id: str
    ) -> dict[str, Any] | None: ...

    def load_window_bearing_results(
        self, summary_window_id: str
    ) -> list[dict[str, Any]]: ...

    def save_window_result(
        self, result: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    def load_expired_open_windows(
        self, *, cutoff_ns: int
    ) -> list[list[dict[str, Any]]]: ...

    def first_received_at_ns(self, summary_window_id: str) -> int | None: ...
