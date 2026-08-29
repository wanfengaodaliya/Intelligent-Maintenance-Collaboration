from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..contracts import build_summary_window_id


SCHEMA_VERSION = 2

OUTBOX_TABLES = (
    "summary_arbitration_outbox",
    "summary_window_publish_outbox",
    "summary_window_sync_outbox",
    "summary_suggestion_outbox",
)

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS summary_bearing_result (
    result_id TEXT PRIMARY KEY,
    summary_window_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    run_id TEXT,
    window_start_sequence INTEGER NOT NULL,
    window_end_sequence INTEGER NOT NULL,
    bearing_id TEXT NOT NULL,
    edge_node_id TEXT NOT NULL,
    decision_round_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at_ns INTEGER NOT NULL,
    UNIQUE(summary_window_id, bearing_id),
    UNIQUE(summary_window_id, edge_node_id)
);
CREATE INDEX IF NOT EXISTS idx_summary_bearing_window
    ON summary_bearing_result(summary_window_id, bearing_id);

CREATE TABLE IF NOT EXISTS summary_window_result (
    summary_result_id TEXT PRIMARY KEY,
    summary_window_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    run_id TEXT,
    window_start_sequence INTEGER NOT NULL,
    window_end_sequence INTEGER NOT NULL,
    result_status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    has_conflict INTEGER NOT NULL,
    excluded_from_formal_metrics INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_window_device
    ON summary_window_result(device_id, created_at_ns DESC);

CREATE TABLE IF NOT EXISTS summary_arbitration_outbox (
    request_id TEXT PRIMARY KEY,
    conflict_id TEXT NOT NULL UNIQUE,
    summary_result_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    acknowledged_at_ns INTEGER,
    cloud_result_json TEXT
);

CREATE TABLE IF NOT EXISTS summary_window_publish_outbox (
    request_id TEXT PRIMARY KEY,
    summary_result_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    acknowledged_at_ns INTEGER,
    cloud_result_json TEXT,
    UNIQUE(summary_result_id, revision)
);

CREATE TABLE IF NOT EXISTS summary_window_sync_outbox (
    request_id TEXT PRIMARY KEY,
    summary_result_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    acknowledged_at_ns INTEGER,
    cloud_result_json TEXT,
    UNIQUE(summary_result_id, revision)
);

CREATE TABLE IF NOT EXISTS summary_suggestion_outbox (
    request_id TEXT PRIMARY KEY,
    summary_result_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    acknowledged_at_ns INTEGER,
    cloud_result_json TEXT,
    UNIQUE(summary_result_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_summary_outbox_due
    ON summary_arbitration_outbox(state, next_attempt_at_ns);
CREATE INDEX IF NOT EXISTS idx_summary_publish_due
    ON summary_window_publish_outbox(state, next_attempt_at_ns);
CREATE INDEX IF NOT EXISTS idx_summary_sync_due
    ON summary_window_sync_outbox(state, next_attempt_at_ns);
CREATE INDEX IF NOT EXISTS idx_summary_suggestion_due
    ON summary_suggestion_outbox(state, next_attempt_at_ns);

CREATE TABLE IF NOT EXISTS summary_suggestion_task (
    summary_result_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    source_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at_ns INTEGER NOT NULL,
    updated_at_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_suggestion_task_due
    ON summary_suggestion_task(state, next_attempt_at_ns);

CREATE TABLE IF NOT EXISTS summary_metrics_counter (
    metric TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}


def migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Rebuild the v1 schema with node-dimension identity (payload preserved)."""

    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if table_exists(connection, "summary_bearing_result") and (
            "summary_window_id" not in columns(connection, "summary_bearing_result")
        ):
            connection.execute(
                "ALTER TABLE summary_bearing_result RENAME TO summary_bearing_result_legacy_v1"
            )
            connection.executescript(SCHEMA_V2)
            rows = connection.execute(
                "SELECT result_id, device_id, window_start_sequence, "
                "window_end_sequence, bearing_id, payload_json, received_at_ns "
                "FROM summary_bearing_result_legacy_v1"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    continue
                run_id = payload.get("run_id")
                window_id = build_summary_window_id(
                    payload.get("device_id", row["device_id"]),
                    run_id,
                    payload.get(
                        "window_start_sequence", row["window_start_sequence"]
                    ),
                    payload.get("window_end_sequence", row["window_end_sequence"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO summary_bearing_result (
                        result_id, summary_window_id, device_id, run_id,
                        window_start_sequence, window_end_sequence, bearing_id,
                        edge_node_id, decision_round_id, payload_json, received_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["result_id"],
                        window_id,
                        row["device_id"],
                        run_id,
                        row["window_start_sequence"],
                        row["window_end_sequence"],
                        row["bearing_id"],
                        str(payload.get("edge_node_id", "")),
                        str(payload.get("decision_round_id", "")),
                        row["payload_json"],
                        row["received_at_ns"],
                    ),
                )
            connection.execute("DROP TABLE summary_bearing_result_legacy_v1")

        if table_exists(connection, "summary_window_result") and (
            "summary_window_id" not in columns(connection, "summary_window_result")
        ):
            connection.execute(
                "ALTER TABLE summary_window_result RENAME TO summary_window_result_legacy_v1"
            )
            connection.executescript(SCHEMA_V2)
            rows = connection.execute(
                "SELECT * FROM summary_window_result_legacy_v1"
            ).fetchall()
            existing_columns = set(rows[0].keys()) if rows else set()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    continue
                run_id = payload.get("run_id")
                window_id = build_summary_window_id(
                    payload.get("device_id", row["device_id"]),
                    run_id,
                    payload.get(
                        "window_start_sequence", row["window_start_sequence"]
                    ),
                    payload.get("window_end_sequence", row["window_end_sequence"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO summary_window_result (
                        summary_result_id, summary_window_id, device_id, run_id,
                        window_start_sequence, window_end_sequence, result_status,
                        revision, has_conflict, excluded_from_formal_metrics,
                        payload_json, created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        row["summary_result_id"],
                        window_id,
                        row["device_id"],
                        run_id,
                        row["window_start_sequence"],
                        row["window_end_sequence"],
                        row["result_status"],
                        (
                            row["has_conflict"]
                            if "has_conflict" in existing_columns
                            else 0
                        ),
                        (
                            row["excluded_from_formal_metrics"]
                            if "excluded_from_formal_metrics" in existing_columns
                            else 0
                        ),
                        row["payload_json"],
                        row["created_at_ns"],
                    ),
                )
            connection.execute("DROP TABLE summary_window_result_legacy_v1")

        rebuild_outbox_tables(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def rebuild_outbox_tables(connection: sqlite3.Connection) -> None:
    for table in OUTBOX_TABLES:
        if not table_exists(connection, table):
            continue
        if "revision" in columns(connection, table):
            continue
        legacy_table = f"{table}_legacy_v1"
        connection.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")
        connection.executescript(SCHEMA_V2)
        rows = connection.execute(f"SELECT * FROM {legacy_table}").fetchall()
        for row in rows:
            if table == "summary_arbitration_outbox":
                connection.execute(
                    """
                    INSERT OR IGNORE INTO summary_arbitration_outbox (
                        request_id, conflict_id, summary_result_id, revision,
                        payload_json, state, attempts, next_attempt_at_ns,
                        last_error, created_at_ns, acknowledged_at_ns, cloud_result_json
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _outbox_values(row, with_conflict_id=True),
                )
            else:
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {table} (
                        request_id, summary_result_id, revision, payload_json,
                        state, attempts, next_attempt_at_ns, last_error,
                        created_at_ns, acknowledged_at_ns, cloud_result_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _outbox_values(row, with_conflict_id=False),
                )
        connection.execute(f"DROP TABLE {legacy_table}")


def _outbox_values(
    row: sqlite3.Row, *, with_conflict_id: bool
) -> tuple[Any, ...]:
    def pick(name: str) -> Any:
        return row[name] if name in row.keys() else None

    if with_conflict_id:
        return (
            row["request_id"],
            row["conflict_id"],
            row["summary_result_id"],
            row["payload_json"],
            pick("state"),
            pick("attempts"),
            pick("next_attempt_at_ns"),
            pick("last_error"),
            pick("created_at_ns"),
            pick("acknowledged_at_ns"),
            pick("cloud_result_json"),
        )
    return (
        row["request_id"],
        row["summary_result_id"],
        row["payload_json"],
        pick("state"),
        pick("attempts"),
        pick("next_attempt_at_ns"),
        pick("last_error"),
        pick("created_at_ns"),
        pick("acknowledged_at_ns"),
        pick("cloud_result_json"),
    )
