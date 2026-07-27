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
    """Create the current schema and record its first migration once."""
    with connect(database_path) as connection:
        connection.executescript(DDL)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (SCHEMA_VERSION, time.time_ns(), "initial cloud review schema"),
        )
