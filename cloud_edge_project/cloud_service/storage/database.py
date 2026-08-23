"""Connection and schema initialization helpers."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from core.scenario_plugin import StorageProvider

from .schema import DDL, EDGE_PACKET_SUMMARY_DDL, MODEL_UPDATE_TASK_DDL, SCHEMA_VERSION


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


class _SQLiteStorageRegistrar:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute_schema(self, script: str) -> None:
        self._connection.executescript(script)


def initialize_database(
    database_path: Path,
    *,
    storage_providers: Iterable[StorageProvider] | None = None,
) -> None:
    """Create or directly migrate the summary storage to the documented schema."""
    providers = None if storage_providers is None else tuple(storage_providers)
    if providers == ():
        raise ValueError("storage_providers must not be empty when provided")
    with connect(database_path) as connection:
        _migrate_v1_to_sender_schema(connection)
        _migrate_v2_summary_to_document_schema(connection)
        legacy_summary_table = _migrate_v3_summary_to_ingestion_schema(connection)
        _migrate_v10_to_v11_identity_fields(connection)
        _migrate_v15_model_update_table(connection)
        if providers is None:
            connection.executescript(DDL)
        else:
            registrar = _SQLiteStorageRegistrar(connection)
            for provider in providers:
                provider.initialize(registrar)
        _migrate_v5_to_v6(connection)
        _migrate_v10_to_v11_identity_fields(connection)
        if legacy_summary_table:
            _copy_legacy_summaries(connection, legacy_summary_table)
        _migrate_v16_to_v17_fault_labels(connection)
        _migrate_v17_to_v18_moment_edge_label(connection)
        _migrate_v18_to_v19_label_confirmation_risk_level(connection)
        _migrate_v19_to_v20_dual_model_support(connection)
        _migrate_v20_to_v21_model_update_columns(connection)
        _migrate_v21_to_v22_model_update_suggestion(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description=excluded.description",
            (
                11,
                time.time_ns(),
                "device-task-bearing identity fields for cloud review",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description=excluded.description",
            (
                14,
                time.time_ns(),
                "bearing review manifest and exact raw-context request storage",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description=excluded.description",
            (
                12,
                time.time_ns(),
                "global analysis result storage",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description=excluded.description",
            (
                13,
                time.time_ns(),
                "model update task storage",
            ),
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_ns, description) VALUES (?, ?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description=excluded.description",
            (
                SCHEMA_VERSION,
                time.time_ns(),
                "unify abnormal labels to fault",
            ),
        )


def _migrate_v15_model_update_table(connection: sqlite3.Connection) -> None:
    """Atomically migrate v15 rows and resume a previously interrupted migration."""

    current_table = _table_exists(connection, "model_update_task")
    legacy_table = "model_update_task_legacy_v15"
    legacy_exists = _table_exists(connection, legacy_table)
    current_is_v16 = current_table and "problem_id" in _columns(
        connection, "model_update_task"
    )
    if current_is_v16 and not legacy_exists:
        return
    if not current_table and not legacy_exists:
        return
    if current_table and not current_is_v16 and legacy_exists:
        raise sqlite3.IntegrityError(
            "both v15 model_update_task and its recovery table exist"
        )

    # Previous schema migrations may have pending work. Commit that work first so
    # this table rebuild has its own all-or-nothing transaction boundary.
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if current_table and not current_is_v16:
            connection.execute("DROP INDEX IF EXISTS idx_model_update_analysis")
            connection.execute(
                f"ALTER TABLE model_update_task RENAME TO {legacy_table}"
            )
        _create_model_update_task_schema(connection)
        _copy_legacy_model_updates(connection, legacy_table)
        connection.execute(f"DROP TABLE {legacy_table}")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _create_model_update_task_schema(connection: sqlite3.Connection) -> None:
    """Execute the static model-update DDL without SQLite's implicit script commit."""

    for statement in MODEL_UPDATE_TASK_DDL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _copy_legacy_model_updates(
    connection: sqlite3.Connection, legacy_table: str
) -> None:
    """Map candidate-first v15 rows into the final lifecycle contract."""

    rows = connection.execute(f"SELECT * FROM {legacy_table}").fetchall()
    for row in rows:
        candidate = {
            "artifact_path": row["update_file"],
            "artifact_sha256": row["update_file_sha256"],
        }
        status = (
            "handoff_to_distribution"
            if row["status"] == "distribution_prepared"
            else row["status"]
        )
        connection.execute(
            """INSERT OR IGNORE INTO model_update_task(
                   update_id,analysis_id,problem_id,scenario_type,subject_id,
                   problem_type,problem_context_json,evidence_snapshot_json,
                   baseline_version,candidate_version,candidate_artifact_json,
                   status,validation_result_json,confirmation_result_json,
                   distribution_result_json,created_at_ns,updated_at_ns
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["update_id"], row["analysis_id"],
                f"legacy_{row['update_id']}", row["scenario_type"],
                row["subject_id"], "legacy_update", "{}", "{}",
                row["old_version"], row["new_version"],
                _json(candidate), status, row["validation_result_json"],
                row["confirmation_json"], row["distribution_result_json"],
                row["created_at_ns"], row["updated_at_ns"],
            ),
        )


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _migrate_v16_to_v17_fault_labels(connection: sqlite3.Connection) -> None:
    """Rebuild the summary CHECK constraint and rewrite stored abnormal labels."""

    if not _table_exists(connection, "edge_packet_summary"):
        return

    create_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edge_packet_summary'"
    ).fetchone()[0]
    if create_sql and "'abnormal'" in create_sql:
        _rebuild_summary_table_for_fault(connection)
    _rewrite_abnormal_labels(connection)


def _rebuild_summary_table_for_fault(connection: sqlite3.Connection) -> None:
    """Replace the v16 summary table (abnormal CHECK) with the v17 definition."""

    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_alter_table = connection.execute(
        "PRAGMA legacy_alter_table"
    ).fetchone()[0]
    # PRAGMA directives are no-ops inside a transaction: commit any pending
    # work from earlier migrations before changing them.
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        legacy_table = "edge_packet_summary_legacy_v16"
        for index_name in (
            "idx_edge_summary_sender_time",
            "idx_edge_summary_edge_received",
            "idx_edge_summary_device_task_bearing",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        connection.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        connection.execute(
            f"ALTER TABLE edge_packet_summary RENAME TO {legacy_table}"
        )
        for statement in EDGE_PACKET_SUMMARY_DDL.split(";"):
            if statement.strip():
                connection.execute(statement)
        new_columns = [
            item[1]
            for item in connection.execute("PRAGMA table_info(edge_packet_summary)")
        ]
        legacy_columns = {
            item[1]
            for item in connection.execute(f"PRAGMA table_info({legacy_table})")
        }
        copied_columns = [column for column in new_columns if column in legacy_columns]
        selects = []
        for column in copied_columns:
            if column == "edge_result":
                selects.append(
                    "CASE WHEN edge_result = 'abnormal' THEN 'fault' "
                    "ELSE edge_result END"
                )
            elif column == "summary_json":
                selects.append(
                    "REPLACE(summary_json, '\"abnormal\"', '\"fault\"')"
                )
            else:
                selects.append(column)
        connection.execute(
            f"INSERT INTO edge_packet_summary ({','.join(copied_columns)}) "
            f"SELECT {','.join(selects)} FROM {legacy_table}"
        )
        connection.execute(f"DROP TABLE {legacy_table}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"v16 to v17 migration foreign key violations: {violations}"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute(
            f"PRAGMA legacy_alter_table = {int(legacy_alter_table)}"
        )
        connection.execute(f"PRAGMA foreign_keys = {int(foreign_keys)}")


def _migrate_v17_to_v18_moment_edge_label(connection: sqlite3.Connection) -> None:
    """Add the edge_label column to cloud_moment_review_record (idempotent)."""

    if not _table_exists(connection, "cloud_moment_review_record"):
        return
    if "edge_label" in _columns(connection, "cloud_moment_review_record"):
        return
    connection.execute("ALTER TABLE cloud_moment_review_record ADD COLUMN edge_label TEXT")


def _migrate_v18_to_v19_label_confirmation_risk_level(
    connection: sqlite3.Connection,
) -> None:
    """Add the confirmed_risk_level column to label_confirmation (idempotent)."""

    if not _table_exists(connection, "label_confirmation"):
        return
    if "confirmed_risk_level" in _columns(connection, "label_confirmation"):
        return
    connection.execute(
        "ALTER TABLE label_confirmation ADD COLUMN confirmed_risk_level TEXT"
    )


def _migrate_v19_to_v20_dual_model_support(connection: sqlite3.Connection) -> None:
    """Add the model_type column to model_update_task (idempotent)."""

    if not _table_exists(connection, "model_update_task"):
        return
    if "model_type" in _columns(connection, "model_update_task"):
        return
    connection.execute(
        "ALTER TABLE model_update_task ADD COLUMN model_type TEXT NOT NULL DEFAULT 'distilled_h5'"
    )


def _migrate_v20_to_v21_model_update_columns(connection: sqlite3.Connection) -> None:
    """Add trainer_plan_json and rollback_result_json columns (idempotent)."""

    if not _table_exists(connection, "model_update_task"):
        return
    columns = _columns(connection, "model_update_task")
    if "trainer_plan_json" not in columns:
        connection.execute(
            "ALTER TABLE model_update_task ADD COLUMN trainer_plan_json TEXT"
        )
    if "rollback_result_json" not in columns:
        connection.execute(
            "ALTER TABLE model_update_task ADD COLUMN rollback_result_json TEXT"
        )


def _migrate_v21_to_v22_model_update_suggestion(connection: sqlite3.Connection) -> None:
    """Add the suggestion_json column to model_update_task (idempotent)."""

    if not _table_exists(connection, "model_update_task"):
        return
    if "suggestion_json" not in _columns(connection, "model_update_task"):
        connection.execute(
            "ALTER TABLE model_update_task ADD COLUMN suggestion_json TEXT"
        )


def _rewrite_abnormal_labels(connection: sqlite3.Connection) -> None:
    """Unify historical abnormal values to fault in unconstrained columns."""

    plain_updates = (
        ("bearing_review", ("edge_state",)),
        ("bearing_task_result", ("edge_state", "cloud_state", "bearing_state")),
        ("device_task_result", ("final_state",)),
    )
    for table, columns in plain_updates:
        if not _table_exists(connection, table):
            continue
        existing = _columns(connection, table)
        for column in columns:
            if column not in existing:
                continue
            connection.execute(
                f"UPDATE {table} SET {column} = 'fault' WHERE {column} = 'abnormal'"
            )

    json_updates = (
        ("diagnosis_events", ("result_json", "human_review_json")),
        ("device_arbitration_record", ("request_json", "result_json")),
        ("global_analysis_result", ("result_json",)),
        ("enhanced_analysis_result", ("result_json",)),
        ("final_diagnosis_summary", ("summary_json",)),
        ("bearing_review", ("result_json",)),
        ("bearing_task_result", ("result_json",)),
        ("device_task_result", ("result_json",)),
        (
            "model_update_task",
            (
                "validation_result_json",
                "confirmation_result_json",
                "distribution_result_json",
                "post_validation_result_json",
            ),
        ),
        ("workflow_review_job", ("request_json", "raw_batch_json", "result_json")),
    )
    for table, columns in json_updates:
        if not _table_exists(connection, table):
            continue
        existing = _columns(connection, table)
        for column in columns:
            if column not in existing:
                continue
            connection.execute(
                f"UPDATE {table} SET {column} = "
                "REPLACE("
                + column
                + ", '\"abnormal\"', '\"fault\"') "
                f"WHERE {column} LIKE '%abnormal%'"
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


def _migrate_v10_to_v11_identity_fields(connection: sqlite3.Connection) -> None:
    """Add nullable cloud-review business identity fields to an existing v10 database."""

    additions = {
        "senders": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
        "edge_packet_summary": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
        "raw_packet_index": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
        "cloud_review": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
        "raw_context_request": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
        "bearing_configuration": (("device_id", "TEXT"), ("bearing_id", "TEXT")),
    }
    for table, columns in additions.items():
        if not _table_exists(connection, table):
            continue
        existing = _columns(connection, table)
        for name, definition in columns:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    if not _table_exists(connection, "senders"):
        return
    for table in (
        "edge_packet_summary",
        "raw_packet_index",
        "cloud_review",
        "raw_context_request",
        "bearing_configuration",
    ):
        if not _table_exists(connection, table):
            continue
        connection.execute(
            f"UPDATE {table} SET "
            "device_id=(SELECT device_id FROM senders WHERE senders.sender_id="
            f"{table}.sender_id), "
            "bearing_id=(SELECT bearing_id FROM senders WHERE senders.sender_id="
            f"{table}.sender_id) "
            "WHERE device_id IS NULL AND bearing_id IS NULL "
            "AND EXISTS (SELECT 1 FROM senders WHERE senders.sender_id="
            f"{table}.sender_id AND senders.device_id IS NOT NULL "
            "AND senders.bearing_id IS NOT NULL)"
        )

    _create_v11_identity_indexes(connection)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}


def _create_v11_identity_indexes(connection: sqlite3.Connection) -> None:
    indexes = (
        (
            "idx_edge_summary_device_task_bearing",
            "edge_packet_summary(device_id,task_id,bearing_id,end_timestamp_ns)",
        ),
        (
            "idx_raw_packet_device_bearing_time",
            "raw_packet_index(device_id,bearing_id,end_generate_timestamp_ns)",
        ),
        (
            "idx_cloud_review_device_task_bearing",
            "cloud_review(device_id,task_id,bearing_id,updated_at_ns)",
        ),
        (
            "idx_bearing_configuration_subject_time",
            "bearing_configuration(device_id,bearing_id,effective_from_ns)",
        ),
    )
    for name, target in indexes:
        table = target.split("(", 1)[0]
        if _table_exists(connection, table):
            connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    """Rebuild v5 context tables while preserving requests and review links."""

    columns = {
        item[1]
        for item in connection.execute(
            "PRAGMA table_info(raw_context_request)"
        )
    }
    if "minimum_context_packet_count" in columns:
        return

    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_alter_table = connection.execute(
        "PRAGMA legacy_alter_table"
    ).fetchone()[0]
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE cloud_review_v6 (
                review_id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                anchor_packet_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                feature_extractor_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                review_status TEXT NOT NULL CHECK (
                    review_status IN (
                        'preliminary', 'complete',
                        'insufficient_context', 'invalid'
                    )
                ),
                context_status TEXT NOT NULL CHECK (
                    context_status IN (
                        'pending_context', 'partial_context', 'complete',
                        'insufficient_context', 'not_requested', 'invalid'
                    )
                ),
                data_quality_valid INTEGER NOT NULL CHECK (
                    data_quality_valid IN (0, 1)
                ),
                start_timestamp_ns INTEGER,
                end_timestamp_ns INTEGER,
                packet_count INTEGER NOT NULL DEFAULT 1 CHECK (
                    packet_count > 0
                ),
                data_quality_json TEXT NOT NULL,
                cloud_recomputed_features_json TEXT,
                cloud_enhanced_features_json TEXT,
                advanced_features_json TEXT,
                context_features_json TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                UNIQUE (
                    sender_id, anchor_packet_id, feature_extractor_version
                ),
                FOREIGN KEY (sender_id, anchor_packet_id)
                    REFERENCES edge_packet_summary(sender_id, packet_id),
                CHECK (json_valid(data_quality_json)),
                CHECK (
                    cloud_recomputed_features_json IS NULL
                    OR json_valid(cloud_recomputed_features_json)
                ),
                CHECK (
                    cloud_enhanced_features_json IS NULL
                    OR json_valid(cloud_enhanced_features_json)
                ),
                CHECK (
                    advanced_features_json IS NULL
                    OR json_valid(advanced_features_json)
                ),
                CHECK (
                    context_features_json IS NULL
                    OR json_valid(context_features_json)
                )
            );
            CREATE TABLE raw_context_request_v6 (
                request_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                anchor_packet_id TEXT NOT NULL,
                anchor_sequence_number INTEGER NOT NULL CHECK (
                    anchor_sequence_number > 0
                ),
                before_packet_count INTEGER NOT NULL CHECK (
                    before_packet_count > 0
                ),
                after_packet_count INTEGER NOT NULL CHECK (
                    after_packet_count >= 0
                ),
                minimum_context_packet_count INTEGER NOT NULL CHECK (
                    minimum_context_packet_count > 0
                ),
                request_status TEXT NOT NULL CHECK (
                    request_status IN (
                        'created', 'dispatched', 'pending_context',
                        'partial_context', 'complete',
                        'insufficient_context', 'dispatch_failed'
                    )
                ),
                requested_at_ns INTEGER NOT NULL,
                deadline_at_ns INTEGER NOT NULL,
                edge_response_json TEXT,
                last_error_code TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                FOREIGN KEY (review_id)
                    REFERENCES cloud_review(review_id) ON DELETE CASCADE,
                CHECK (deadline_at_ns > requested_at_ns),
                CHECK (
                    edge_response_json IS NULL
                    OR json_valid(edge_response_json)
                )
            );
            INSERT INTO cloud_review_v6 (
                review_id, sender_id, anchor_packet_id, task_id,
                feature_extractor_version, schema_version, review_status,
                context_status, data_quality_valid, start_timestamp_ns,
                end_timestamp_ns, packet_count, data_quality_json,
                cloud_recomputed_features_json,
                cloud_enhanced_features_json, advanced_features_json,
                context_features_json, created_at_ns, updated_at_ns
            )
            SELECT
                review_id, sender_id, anchor_packet_id, task_id,
                feature_extractor_version, schema_version, review_status,
                context_status, data_quality_valid, start_timestamp_ns,
                end_timestamp_ns, packet_count, data_quality_json,
                cloud_recomputed_features_json,
                cloud_enhanced_features_json, advanced_features_json,
                context_features_json, created_at_ns, updated_at_ns
            FROM cloud_review;
            INSERT INTO raw_context_request_v6 (
                request_id, review_id, task_id, sender_id,
                anchor_packet_id, anchor_sequence_number,
                before_packet_count, after_packet_count,
                minimum_context_packet_count, request_status,
                requested_at_ns, deadline_at_ns, edge_response_json,
                last_error_code, created_at_ns, updated_at_ns
            )
            SELECT
                request_id, review_id, task_id, sender_id,
                anchor_packet_id, anchor_sequence_number,
                before_packet_count, after_packet_count,
                MIN(before_packet_count + after_packet_count, 16),
                request_status, requested_at_ns, deadline_at_ns,
                edge_response_json, last_error_code, created_at_ns,
                updated_at_ns
            FROM raw_context_request;
            DROP INDEX IF EXISTS idx_raw_context_request_deadline;
            DROP INDEX IF EXISTS idx_cloud_review_sender_time;
            DROP TABLE raw_context_request;
            DROP TABLE cloud_review;
            ALTER TABLE cloud_review_v6 RENAME TO cloud_review;
            ALTER TABLE raw_context_request_v6
                RENAME TO raw_context_request;
            CREATE INDEX idx_cloud_review_sender_time
                ON cloud_review(sender_id, updated_at_ns);
            CREATE INDEX idx_raw_context_request_deadline
                ON raw_context_request(request_status, deadline_at_ns);
            """
        )
        violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"v5 to v6 migration foreign key violations: {violations}"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute(
            f"PRAGMA legacy_alter_table = {int(legacy_alter_table)}"
        )
        connection.execute(f"PRAGMA foreign_keys = {int(foreign_keys)}")
