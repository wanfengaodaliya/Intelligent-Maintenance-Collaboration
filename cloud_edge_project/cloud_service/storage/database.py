"""Connection and schema initialization helpers."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import DDL, SCHEMA_VERSION


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a configured connection and always close its SQLite file handle."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: Path) -> None:
    """Create the current schema and preserve legacy device-keyed tables."""
    with connect(database_path) as connection:
        _migrate_v1_to_sender_schema(connection)
        connection.executescript(DDL)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (SCHEMA_VERSION, time.time_ns(), "initial cloud review schema"),
        )


def _migrate_v1_to_sender_schema(connection: sqlite3.Connection) -> None:
    """Retain unmappable v1 device data before creating the sender-keyed schema."""

    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='edge_packet_summary'"
    ).fetchone()
    if row is None:
        return
    columns = {item[1] for item in connection.execute("PRAGMA table_info(edge_packet_summary)")}
    if "sender_id" in columns:
        return
    for table in (
        "review_context_packets", "diagnosis_events", "cloud_review", "raw_packet_index",
        "ingestion_conflicts", "edge_packet_summary", "devices",
    ):
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_v1")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?)",
        (1, time.time_ns(), "legacy device-keyed tables retained as *_legacy_v1"),
    )
