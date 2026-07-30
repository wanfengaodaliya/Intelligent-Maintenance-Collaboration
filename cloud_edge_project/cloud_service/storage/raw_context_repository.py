"""Persistence for raw-context requests and their lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .database import connect


TERMINAL_REQUEST_STATUSES = {
    "complete",
    "partial_context",
    "insufficient_context",
}
PROCESSING_LEASE_PREFIX = "RAW_CONTEXT_PROCESSING_LEASE:"


def _contiguous_before_count(
    positions: set[int],
    before_packet_count: int,
) -> int:
    count = 0
    for position in range(-1, -before_packet_count - 1, -1):
        if position not in positions:
            break
        count += 1
    return count


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
        minimum_context_packet_count: int,
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
                "minimum_context_packet_count,request_status,requested_at_ns,"
                "deadline_at_ns,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id, review_id, task_id, sender_id, anchor_packet_id,
                    anchor_sequence_number, before_packet_count, after_packet_count,
                    minimum_context_packet_count, "created",
                    requested_at_ns, deadline_at_ns,
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

    def acquire_processing_lease(
        self,
        request_id: str,
        *,
        lease_until_ns: int,
        updated_at_ns: int,
    ) -> bool:
        marker = f"{PROCESSING_LEASE_PREFIX}{lease_until_ns}"
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT request_status,last_error_code "
                "FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown request_id: {request_id}")
            if (
                current["request_status"] in TERMINAL_REQUEST_STATUSES
                or _processing_lease_until(current["last_error_code"])
                is not None
            ):
                return False
            connection.execute(
                "UPDATE raw_context_request SET last_error_code=?,"
                "updated_at_ns=? WHERE request_id=?",
                (marker, updated_at_ns, request_id),
            )
        return True

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
            if current["request_status"] in TERMINAL_REQUEST_STATUSES:
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
            if current["request_status"] in TERMINAL_REQUEST_STATUSES:
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

    def mark_partial(
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
            if current["request_status"] in TERMINAL_REQUEST_STATUSES:
                return dict(current)
            self._mark_partial(
                connection,
                request_id=request_id,
                review_id=current["review_id"],
                updated_at_ns=now,
            )
            row = connection.execute(
                "SELECT * FROM raw_context_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row)

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
            if current["request_status"] in TERMINAL_REQUEST_STATUSES:
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
                "SELECT request_id,review_id,before_packet_count,"
                "minimum_context_packet_count,last_error_code "
                "FROM raw_context_request "
                "WHERE request_status IN ('created','dispatched','pending_context','dispatch_failed') "
                "AND deadline_at_ns < ? ORDER BY request_id",
                (now,),
            ).fetchall()
            request_ids = []
            for row in rows:
                lease_until_ns = _processing_lease_until(
                    row["last_error_code"]
                )
                if lease_until_ns is not None and now <= lease_until_ns:
                    continue
                request_ids.append(row["request_id"])
                positions = {
                    item["relative_position"]
                    for item in connection.execute(
                        "SELECT relative_position FROM review_context_packets "
                        "WHERE review_id=?",
                        (row["review_id"],),
                    ).fetchall()
                }
                contiguous_count = _contiguous_before_count(
                    positions,
                    row["before_packet_count"],
                )
                if contiguous_count == row["before_packet_count"]:
                    connection.execute(
                        "UPDATE raw_context_request SET "
                        "request_status='complete', last_error_code=NULL, "
                        "updated_at_ns=? WHERE request_id=?",
                        (now, row["request_id"]),
                    )
                    connection.execute(
                        "UPDATE cloud_review SET context_status='complete', "
                        "updated_at_ns=? WHERE review_id=?",
                        (now, row["review_id"]),
                    )
                elif (
                    row["minimum_context_packet_count"]
                    <= contiguous_count
                    < row["before_packet_count"]
                ):
                    self._mark_partial(
                        connection,
                        request_id=row["request_id"],
                        review_id=row["review_id"],
                        updated_at_ns=now,
                    )
                else:
                    connection.execute(
                        "UPDATE raw_context_request SET "
                        "request_status='insufficient_context', "
                        "last_error_code='CONTEXT_DEADLINE_EXCEEDED', "
                        "updated_at_ns=? WHERE request_id=?",
                        (now, row["request_id"]),
                    )
                    connection.execute(
                        "UPDATE cloud_review SET "
                        "review_status='insufficient_context', "
                        "context_status='insufficient_context', "
                        "updated_at_ns=? WHERE review_id=?",
                        (now, row["review_id"]),
                    )
        return request_ids

    @staticmethod
    def _mark_partial(
        connection: Any,
        *,
        request_id: str,
        review_id: str,
        updated_at_ns: int,
    ) -> None:
        connection.execute(
            "UPDATE raw_context_request SET request_status='partial_context', "
            "last_error_code='CONTEXT_DEADLINE_EXCEEDED', updated_at_ns=? "
            "WHERE request_id=?",
            (updated_at_ns, request_id),
        )
        connection.execute(
            "UPDATE cloud_review SET context_status='partial_context', "
            "updated_at_ns=? WHERE review_id=?",
            (updated_at_ns, review_id),
        )


def _processing_lease_until(error_code: str | None) -> int | None:
    if (
        not isinstance(error_code, str)
        or not error_code.startswith(PROCESSING_LEASE_PREFIX)
    ):
        return None
    try:
        return int(error_code.removeprefix(PROCESSING_LEASE_PREFIX))
    except ValueError:
        return None
