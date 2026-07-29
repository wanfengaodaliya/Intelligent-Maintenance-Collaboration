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
    """Create or directly migrate the summary storage to the documented schema."""
    with connect(database_path) as connection:
        _migrate_v1_to_sender_schema(connection)
        _migrate_v2_summary_to_document_schema(connection)
        legacy_summary_table = _migrate_v3_summary_to_ingestion_schema(connection)
        connection.executescript(DDL)
        if legacy_summary_table:
            _copy_legacy_summaries(connection, legacy_summary_table)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?)",
            (
                SCHEMA_VERSION,
                time.time_ns(),
                "raw context request and ingestion schema",
            ),
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


def _migrate_v2_summary_to_document_schema(connection: sqlite3.Connection) -> None:
    """Add the documented fields in place and preserve existing sender summaries."""

    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='edge_packet_summary'"
    ).fetchone()
    if row is None:
        return

    columns = {item[1] for item in connection.execute("PRAGMA table_info(edge_packet_summary)")}
    if "end_timestamp_ns" in columns:
        return
    if "end_generate_timestamp_ns" not in columns:
        return

    connection.execute(
        "ALTER TABLE edge_packet_summary RENAME COLUMN end_generate_timestamp_ns TO end_timestamp_ns"
    )
    connection.execute(
        "ALTER TABLE edge_packet_summary RENAME COLUMN feature_generated_at_ns TO summary_generated_at_ns"
    )

    additions = (
        ("edge_node_id", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
        ("vibration_source_sample_rate_hz", "INTEGER NOT NULL DEFAULT 64000"),
        ("vibration_analysis_sample_rate_hz", "INTEGER NOT NULL DEFAULT 16000"),
        ("vibration_unit", "TEXT NOT NULL DEFAULT 'mm/s'"),
        ("current_1_source_sample_rate_hz", "INTEGER NOT NULL DEFAULT 64000"),
        ("current_1_analysis_sample_rate_hz", "INTEGER NOT NULL DEFAULT 16000"),
        ("current_1_unit", "TEXT NOT NULL DEFAULT 'A'"),
        ("current_2_source_sample_rate_hz", "INTEGER NOT NULL DEFAULT 64000"),
        ("current_2_analysis_sample_rate_hz", "INTEGER NOT NULL DEFAULT 16000"),
        ("current_2_unit", "TEXT NOT NULL DEFAULT 'A'"),
        ("shaft_speed_rpm_last", "REAL NOT NULL DEFAULT 0"),
        ("shaft_speed_rpm_minimum", "REAL NOT NULL DEFAULT 0"),
        ("shaft_speed_rpm_maximum", "REAL NOT NULL DEFAULT 0"),
        ("shaft_speed_rpm_standard_deviation", "REAL NOT NULL DEFAULT 0"),
        ("load_torque_nm_last", "REAL NOT NULL DEFAULT 0"),
        ("load_torque_nm_minimum", "REAL NOT NULL DEFAULT 0"),
        ("load_torque_nm_maximum", "REAL NOT NULL DEFAULT 0"),
        ("load_torque_nm_standard_deviation", "REAL NOT NULL DEFAULT 0"),
        ("bearing_radial_load_n_last", "REAL NOT NULL DEFAULT 0"),
        ("bearing_radial_load_n_minimum", "REAL NOT NULL DEFAULT 0"),
        ("bearing_radial_load_n_maximum", "REAL NOT NULL DEFAULT 0"),
        ("bearing_radial_load_n_standard_deviation", "REAL NOT NULL DEFAULT 0"),
        ("edge_result", "TEXT NOT NULL DEFAULT 'warning'"),
        ("confidence", "REAL NOT NULL DEFAULT 0"),
        ("edge_risk_level", "TEXT NOT NULL DEFAULT 'low'"),
    )
    for name, definition in additions:
        connection.execute(f"ALTER TABLE edge_packet_summary ADD COLUMN {name} {definition}")

    for field in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n"):
        connection.execute(
            f"UPDATE edge_packet_summary SET {field}_last={field}_mean, "
            f"{field}_minimum={field}_mean, {field}_maximum={field}_mean"
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_summary_sender_task_sequence "
        "ON edge_packet_summary(sender_id, task_id, sequence_number)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_edge_summary_edge_received "
        "ON edge_packet_summary(edge_node_id, received_at_ns)"
    )


def _migrate_v3_summary_to_ingestion_schema(connection: sqlite3.Connection) -> str | None:
    """Retain v3 summaries while replacing its completed-only table definition."""

    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='edge_packet_summary'"
    ).fetchone()
    if row is None:
        return None
    columns = {item[1] for item in connection.execute("PRAGMA table_info(edge_packet_summary)")}
    if "processing_status" in columns:
        return None
    for index_name in ("idx_edge_summary_sender_time", "idx_edge_summary_edge_received"):
        connection.execute(f"DROP INDEX IF EXISTS {index_name}")
    legacy_table = "edge_packet_summary_legacy_v3"
    connection.execute(f"DROP TABLE IF EXISTS {legacy_table}")
    connection.execute(f"ALTER TABLE edge_packet_summary RENAME TO {legacy_table}")
    return legacy_table


def _copy_legacy_summaries(connection: sqlite3.Connection, legacy_table: str) -> None:
    """Copy completed-only v3 rows into the v4 table without altering their payload."""

    new_columns = [item[1] for item in connection.execute("PRAGMA table_info(edge_packet_summary)")]
    legacy_columns = {item[1] for item in connection.execute(f"PRAGMA table_info({legacy_table})")}
    copied_columns = [column for column in new_columns if column in legacy_columns]
    target_columns = copied_columns + ["processing_status"]
    source_columns = copied_columns + ["'perception_completed'"]
    connection.execute(
        f"INSERT INTO edge_packet_summary ({','.join(target_columns)}) "
        f"SELECT {','.join(source_columns)} FROM {legacy_table}"
    )
