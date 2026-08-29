from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from ..contracts import canonical_json, stable_id


def due_tasks(
    connection: sqlite3.Connection, *, now_ns: int, limit: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT summary_result_id, revision, attempts, source_json
        FROM summary_suggestion_task
        WHERE state IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at_ns <= ?
        ORDER BY created_at_ns, summary_result_id
        LIMIT ?
        """,
        (int(now_ns), int(limit)),
    ).fetchall()
    return [
        {
            "summary_result_id": row["summary_result_id"],
            "revision": int(row["revision"]),
            "attempts": int(row["attempts"]),
            "source": json.loads(row["source_json"]),
        }
        for row in rows
    ]


def complete_task(
    connection: sqlite3.Connection,
    summary_result_id: str,
    suggestion: Mapping[str, Any],
    *,
    now_ns: int,
) -> None:
    task = connection.execute(
        "SELECT revision, state FROM summary_suggestion_task WHERE summary_result_id = ?",
        (str(summary_result_id),),
    ).fetchone()
    if task is None or task["state"] == "COMPLETED":
        return
    revision = int(task["revision"])
    connection.execute(
        """
        INSERT OR IGNORE INTO summary_suggestion_outbox (
            request_id, summary_result_id, revision, payload_json,
            state, attempts, next_attempt_at_ns, created_at_ns
        ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, ?)
        """,
        (
            stable_id("publish-suggestion", summary_result_id, revision),
            str(summary_result_id),
            revision,
            canonical_json(suggestion),
            int(suggestion["created_at_ns"]),
        ),
    )
    connection.execute(
        """
        UPDATE summary_suggestion_task
        SET state = 'COMPLETED', updated_at_ns = ?, last_error = NULL
        WHERE summary_result_id = ?
        """,
        (int(now_ns), str(summary_result_id)),
    )


def defer_task(
    connection: sqlite3.Connection,
    summary_result_id: str,
    *,
    error: str,
    attempts: int,
    next_attempt_at_ns: int,
    dead_letter: bool,
    now_ns: int,
) -> None:
    connection.execute(
        """
        UPDATE summary_suggestion_task
        SET state = ?, attempts = ?, next_attempt_at_ns = ?,
            last_error = ?, updated_at_ns = ?
        WHERE summary_result_id = ?
        """,
        (
            "DEAD_LETTER" if dead_letter else "RETRY_WAIT",
            int(attempts),
            int(next_attempt_at_ns),
            str(error)[:1000],
            int(now_ns),
            str(summary_result_id),
        ),
    )


def get_suggestion(
    connection: sqlite3.Connection, summary_result_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT payload_json FROM summary_suggestion_outbox "
        "WHERE summary_result_id = ? ORDER BY revision DESC LIMIT 1",
        (str(summary_result_id),),
    ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None
