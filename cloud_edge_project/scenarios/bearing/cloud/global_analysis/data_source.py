"""Data-source protocol and deterministic in-memory implementation."""

from __future__ import annotations

from typing import Any, Protocol


class GlobalAnalysisDataSource(Protocol):
    def load(self, device_id: str, task_limit: int) -> dict[str, Any]:
        ...


class FakeGlobalAnalysisDataSource:
    """In-memory normalized history used by tests and local demonstrations."""

    def __init__(
        self,
        *,
        device_tasks: list[dict[str, Any]] | None = None,
        bearing_tasks: list[dict[str, Any]] | None = None,
        packet_review_pairs: list[dict[str, Any]] | None = None,
        bearing_review_pairs: list[dict[str, Any]] | None = None,
        arbitrations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._rows = {
            "device_tasks": list(device_tasks or []),
            "bearing_tasks": list(bearing_tasks or []),
            "packet_review_pairs": list(packet_review_pairs or []),
            "bearing_review_pairs": list(bearing_review_pairs or []),
            "arbitrations": list(arbitrations or []),
        }
        self._availability = {
            "device_tasks": device_tasks is not None,
            "bearing_tasks": bearing_tasks is not None,
            "packet_review_pairs": packet_review_pairs is not None,
            "bearing_review_pairs": bearing_review_pairs is not None,
            "arbitrations": arbitrations is not None,
        }

    def load(self, device_id: str, task_limit: int) -> dict[str, Any]:
        device_rows = sorted(
            (row for row in self._rows["device_tasks"] if row.get("device_id") == device_id),
            key=lambda row: row.get("completed_at_ns", 0),
        )[-task_limit:]
        task_ids = {row.get("task_id") for row in device_rows}
        result: dict[str, Any] = {"device_tasks": device_rows, "availability": self._availability.copy()}
        for name in ("bearing_tasks", "packet_review_pairs", "bearing_review_pairs", "arbitrations"):
            result[name] = [
                row for row in self._rows[name]
                if row.get("device_id") == device_id and row.get("task_id") in task_ids
            ]
        return result
