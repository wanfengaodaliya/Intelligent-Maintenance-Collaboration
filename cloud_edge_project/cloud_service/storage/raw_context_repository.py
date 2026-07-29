"""Persistence for raw-context requests and their lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .database import connect


class RawContextRequestRepository:
    """Store one stable raw-context request per cloud review."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def create_or_get(
        self,
        *,
        request_id: str,
        review_id: str,
        task_id: str,
        sender_id: str,
        anchor_packet_id: str,
        anchor_sequence_number: int,
        before_packet_count: int,
        after_packet_count: int,
        requested_at_ns: int,
        deadline_at_ns: int,
    ) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM raw_context_request WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if existing:
                return dict(existing)
            connection.execute(
                "INSERT INTO raw_context_request("
                "request_id,review_id,task_id,sender_id,anchor_packet_id,"
                "anchor_sequence_number,before_packet_count,after_packet_count,"
                "request_status,requested_at_ns,deadline_at_ns,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id, review_id, task_id, sender_id, anchor_packet_id,
                    anchor_sequence_number, before_packet_count, after_packet_count,
                    "created", requested_at_ns, deadline_at_ns,
                    requested_at_ns, requested_at_ns,
                ),
            )
            connection.execute(
                "UPDATE cloud_review SET review_status='preliminary', "
                "context_status='pending_context', updated_at_ns=? WHERE review_id=?",
                (requested_at_ns, review_id),
            )
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            updated = dict(row)
        return updated

    def get(self, request_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_dispatch(
        self,
        request_id: str,
        *,
        request_status: str,
        edge_response: dict[str, Any] | None = None,
        last_error_code: str | None = None,
        updated_at_ns: int | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns() if updated_at_ns is None else updated_at_ns
        serialized = (
            json.dumps(
                edge_response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if edge_response is not None
            else None
        )
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown request_id: {request_id}")
            if current["request_status"] in {
                "complete",
                "insufficient_context",
            }:
                return dict(current)
            result = connection.execute(
                "UPDATE raw_context_request SET request_status=?,"
                "edge_response_json=COALESCE(?,edge_response_json),"
                "last_error_code=?,updated_at_ns=? WHERE request_id=?",
                (
                    request_status,
                    serialized,
                    last_error_code,
                    now,
                    request_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row)

    def mark_insufficient(
        self,
        request_id: str,
        *,
        error_code: str,
        edge_response: dict[str, Any] | None = None,
        updated_at_ns: int | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns() if updated_at_ns is None else updated_at_ns
        serialized = (
            json.dumps(
                edge_response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if edge_response is not None
            else None
        )
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown request_id: {request_id}")
            if current["request_status"] in {
                "complete",
                "insufficient_context",
            }:
                return dict(current)
            connection.execute(
                "UPDATE raw_context_request SET "
                "request_status='insufficient_context',"
                "edge_response_json=COALESCE(?,edge_response_json),"
                "last_error_code=?,updated_at_ns=? WHERE request_id=?",
                (serialized, error_code, now, request_id),
            )
            connection.execute(
                "UPDATE cloud_review SET review_status='insufficient_context',"
                "context_status='insufficient_context',updated_at_ns=? WHERE review_id=?",
                (now, current["review_id"]),
            )
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            )
            updated = dict(row.fetchone())
        return updated

    def mark_complete(
        self,
        request_id: str,
        *,
        updated_at_ns: int | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns() if updated_at_ns is None else updated_at_ns
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown request_id: {request_id}")
            if current["request_status"] in {
                "complete",
                "insufficient_context",
            }:
                return dict(current)
            connection.execute(
                "UPDATE raw_context_request SET request_status='complete',"
                "last_error_code=NULL,updated_at_ns=? WHERE request_id=?",
                (now, request_id),
            )
            connection.execute(
                "UPDATE cloud_review SET context_status='complete',"
                "updated_at_ns=? WHERE review_id=?",
                (now, current["review_id"]),
            )
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            )
            updated = dict(row.fetchone())
        return updated

    def expire_due(self, *, now_ns: int | None = None) -> list[str]:
        now = time.time_ns() if now_ns is None else now_ns
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT request_id,review_id FROM raw_context_request "
                "WHERE request_status IN ('created','dispatched','pending_context','dispatch_failed') "
                "AND deadline_at_ns < ? ORDER BY request_id",
                (now,),
            ).fetchall()
            request_ids = [row["request_id"] for row in rows]
            for row in rows:
                connection.execute(
                    "UPDATE raw_context_request SET request_status='insufficient_context', "
                    "last_error_code='CONTEXT_DEADLINE_EXCEEDED', updated_at_ns=? "
                    "WHERE request_id=?",
                    (now, row["request_id"]),
                )
                connection.execute(
                    "UPDATE cloud_review SET review_status='insufficient_context', "
                    "context_status='insufficient_context', updated_at_ns=? "
                    "WHERE review_id=?",
                    (now, row["review_id"]),
                )
        return request_ids
