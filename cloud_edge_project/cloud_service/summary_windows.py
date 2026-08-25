from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError
from cloud_service.storage.database import connect, initialize_database


class SummaryWindowRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def accept(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_summary_window(payload)
        payload_json = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO summary_window_record (
                    summary_result_id, device_id, window_start_sequence,
                    window_end_sequence, result_status, has_conflict,
                    excluded_from_formal_metrics, max_cross_edge_grade_gap,
                    conflicting_pair_count, payload_hash, payload_json, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(summary_result_id) DO NOTHING
                """,
                (
                    normalized["summary_result_id"],
                    normalized["device_id"],
                    normalized["window_start_sequence"],
                    normalized["window_end_sequence"],
                    normalized["result_status"],
                    int(normalized["has_conflict"]),
                    int(normalized["excluded_from_formal_metrics"]),
                    normalized["max_cross_edge_grade_gap"],
                    normalized["conflicting_pair_count"],
                    payload_hash,
                    payload_json,
                    normalized["closed_at_ns"],
                ),
            )
            row = connection.execute(
                """
                SELECT payload_hash, payload_json FROM summary_window_record
                WHERE summary_result_id = ?
                """,
                (normalized["summary_result_id"],),
            ).fetchone()
        if row is None:
            raise RuntimeError("summary window was not persisted")
        if row["payload_hash"] != payload_hash:
            raise ArbitrationPayloadConflictError(
                "summary_result_id already belongs to a different window payload"
            )
        return json.loads(row["payload_json"])

    def list_recent(
        self, *, device_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            if device_id:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM summary_window_record
                    WHERE device_id = ? ORDER BY created_at_ns DESC LIMIT ?
                    """,
                    (device_id, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM summary_window_record
                    ORDER BY created_at_ns DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]


def normalize_summary_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "summary_result_id",
        "device_id",
        "window_start_sequence",
        "window_end_sequence",
        "result_status",
        "has_conflict",
        "excluded_from_formal_metrics",
        "max_cross_edge_grade_gap",
        "conflicting_pair_count",
        "closed_at_ns",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"missing summary-window fields: {missing}")
    result = {field: payload[field] for field in required}
    for field in ("summary_result_id", "device_id"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(f"{field} is required")
        result[field] = result[field].strip()
    if result["result_status"] not in {"FINAL", "PENDING_ARBITRATION", "INCOMPLETE"}:
        raise ValueError("result_status is not supported")
    for field in ("has_conflict", "excluded_from_formal_metrics"):
        if not isinstance(result[field], bool):
            raise ValueError(f"{field} must be boolean")
    for field in (
        "window_start_sequence",
        "window_end_sequence",
        "max_cross_edge_grade_gap",
        "conflicting_pair_count",
        "closed_at_ns",
    ):
        if isinstance(result[field], bool) or not isinstance(result[field], int):
            raise ValueError(f"{field} must be an integer")
        if result[field] < 0:
            raise ValueError(f"{field} must be non-negative")
    if result["window_start_sequence"] < 1:
        raise ValueError("window_start_sequence must be positive")
    if result["window_end_sequence"] < result["window_start_sequence"]:
        raise ValueError("window_end_sequence must not precede window_start_sequence")
    if result["has_conflict"] and result["max_cross_edge_grade_gap"] < 2:
        raise ValueError("conflicted window must have a cross-edge grade gap of at least 2")
    if result["excluded_from_formal_metrics"] and result["has_conflict"]:
        raise ValueError("an incomplete window cannot be counted as a conflict")
    return result
