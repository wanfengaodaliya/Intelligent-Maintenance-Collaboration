from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.action_level_contract import (
    ACTION_SCORER_VERSION,
    ACTION_TO_LEVEL,
    CONFLICT_SEMANTICS,
)

from .aggregation import build_arbitration_request
from .contracts import (
    BINARY_BEARING_STATES,
    GRADE_BY_ACTION,
    build_summary_window_id,
    canonical_json,
    stable_id,
)

SCHEMA_VERSION = 2

_OUTBOX_TABLES = (
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


class BearingResultConflictError(ValueError):
    """A different bearing result already occupies this window slot."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SummaryRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_legacy_tables = _table_exists(connection, "summary_bearing_result")
            legacy_schema = has_legacy_tables and (
                "summary_window_id"
                not in _columns(connection, "summary_bearing_result")
            )
            if legacy_schema and version < SCHEMA_VERSION:
                self._backup_database()
                _migrate_v1_to_v2(connection)
            connection.executescript(SCHEMA_V2)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _backup_database(self) -> None:
        source = Path(self.database_path)
        if source.exists():
            shutil.copy2(source, source.with_name(source.name + ".v1.bak"))

    # ------------------------------------------------------------------
    # Bearing-result persistence (node-dimension uniqueness)
    # ------------------------------------------------------------------

    def save_bearing_result(
        self, result: Mapping[str, Any], *, received_at_ns: int
    ) -> bool:
        """Insert a bearing result; False means an idempotent redelivery."""

        payload_json = canonical_json(result)
        values = (
            str(result["result_id"]),
            str(result["summary_window_id"]),
            str(result["device_id"]),
            result.get("run_id"),
            int(result["window_start_sequence"]),
            int(result["window_end_sequence"]),
            str(result["bearing_id"]),
            str(result["edge_node_id"]),
            str(result["decision_round_id"]),
            payload_json,
            int(received_at_ns),
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO summary_bearing_result (
                        result_id, summary_window_id, device_id, run_id,
                        window_start_sequence, window_end_sequence, bearing_id,
                        edge_node_id, decision_round_id, payload_json, received_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return True
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT payload_json FROM summary_bearing_result WHERE result_id = ?",
                    (values[0],),
                ).fetchone()
                if existing is not None:
                    if existing["payload_json"] == payload_json:
                        return False
                    raise BearingResultConflictError(
                        "bearing result identity conflicts with an existing result",
                        reason="result_id",
                    ) from None
                bearing_slot = connection.execute(
                    """
                    SELECT result_id FROM summary_bearing_result
                    WHERE summary_window_id = ? AND bearing_id = ?
                    """,
                    (values[1], values[6]),
                ).fetchone()
                if bearing_slot is not None:
                    raise BearingResultConflictError(
                        f"window already contains a result for {values[6]}",
                        reason="bearing_slot",
                    ) from None
                raise BearingResultConflictError(
                    f"window already contains a result from {values[7]}",
                    reason="edge_slot",
                ) from None

    def load_window_bearing_results(
        self, summary_window_id: str
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM summary_bearing_result
                WHERE summary_window_id = ?
                ORDER BY bearing_id
                """,
                (str(summary_window_id),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def first_received_at_ns(self, summary_window_id: str) -> int | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(received_at_ns) AS first FROM summary_bearing_result WHERE summary_window_id = ?",
                (str(summary_window_id),),
            ).fetchone()
        return int(row["first"]) if row is not None and row["first"] is not None else None

    def load_expired_open_windows(self, *, cutoff_ns: int) -> list[list[dict[str, Any]]]:
        with self._lock, self._connect() as connection:
            groups = connection.execute(
                """
                SELECT bearing.summary_window_id
                FROM summary_bearing_result AS bearing
                LEFT JOIN summary_window_result AS window
                  ON window.summary_window_id = bearing.summary_window_id
                WHERE window.summary_result_id IS NULL
                GROUP BY bearing.summary_window_id
                HAVING MIN(bearing.received_at_ns) <= ?
                ORDER BY MIN(bearing.received_at_ns)
                """,
                (int(cutoff_ns),),
            ).fetchall()
        return [
            self.load_window_bearing_results(row["summary_window_id"])
            for row in groups
        ]

    # ------------------------------------------------------------------
    # Window results (revisioned)
    # ------------------------------------------------------------------

    def get_window_result(self, summary_window_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM summary_window_result WHERE summary_window_id = ?",
                (str(summary_window_id),),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def get_window_result_by_id(self, summary_result_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM summary_window_result WHERE summary_result_id = ?",
                (str(summary_result_id),),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def save_window_result(self, result: Mapping[str, Any]) -> dict[str, Any] | None:
        """Persist a window result; each distinct payload bumps the revision."""

        arbitration_request = (
            build_arbitration_request(result) if result.get("has_conflict") else None
        )
        summary_window_id = str(result["summary_window_id"])
        summary_result_id = str(result["summary_result_id"])
        closed_at_ns = int(result["closed_at_ns"])
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT result_status, revision, payload_json "
                "FROM summary_window_result WHERE summary_window_id = ?",
                (summary_window_id,),
            ).fetchone()
            if existing is not None:
                payload = dict(result)
                payload["revision"] = int(existing["revision"])
                if existing["payload_json"] == canonical_json(payload):
                    return None
                existing_status = str(existing["result_status"])
                incoming_status = str(result["result_status"])
                # Window closure runs concurrently with MQTT ingestion. A stale
                # timeout snapshot must never replace a newer settled result.
                # Only a late, complete result may advance an INCOMPLETE window.
                if existing_status != "INCOMPLETE" or incoming_status == "INCOMPLETE":
                    return None
            revision = (
                int(existing["revision"]) + 1 if existing is not None else 1
            )
            payload = dict(result)
            payload["revision"] = revision
            payload_json = canonical_json(payload)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO summary_window_result (
                        summary_result_id, summary_window_id, device_id, run_id,
                        window_start_sequence, window_end_sequence, result_status,
                        revision, has_conflict, excluded_from_formal_metrics,
                        payload_json, created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_result_id,
                        summary_window_id,
                        str(result["device_id"]),
                        result.get("run_id"),
                        int(result["window_start_sequence"]),
                        int(result["window_end_sequence"]),
                        str(result["result_status"]),
                        revision,
                        int(bool(result["has_conflict"])),
                        int(bool(result["excluded_from_formal_metrics"])),
                        payload_json,
                        closed_at_ns,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE summary_window_result
                    SET result_status = ?, revision = ?, has_conflict = ?,
                        excluded_from_formal_metrics = ?, payload_json = ?,
                        created_at_ns = ?
                    WHERE summary_window_id = ?
                    """,
                    (
                        str(result["result_status"]),
                        revision,
                        int(bool(result["has_conflict"])),
                        int(bool(result["excluded_from_formal_metrics"])),
                        payload_json,
                        closed_at_ns,
                        summary_window_id,
                    ),
                )

            self._insert_delivery(
                connection,
                table="summary_window_publish_outbox",
                request_id=stable_id("publish", summary_result_id, revision),
                summary_result_id=summary_result_id,
                revision=revision,
                payload=payload,
                created_at_ns=closed_at_ns,
            )
            self._insert_delivery(
                connection,
                table="summary_window_sync_outbox",
                request_id=stable_id("sync", summary_result_id, revision),
                summary_result_id=summary_result_id,
                revision=revision,
                payload=_sync_projection(payload),
                created_at_ns=closed_at_ns,
            )
            if arbitration_request is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO summary_arbitration_outbox (
                        request_id, conflict_id, summary_result_id, revision,
                        payload_json, state, attempts, next_attempt_at_ns,
                        created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, 0, ?)
                    """,
                    (
                        stable_id(
                            "arbitration", arbitration_request["conflict_id"]
                        ),
                        arbitration_request["conflict_id"],
                        summary_result_id,
                        revision,
                        canonical_json(arbitration_request),
                        closed_at_ns,
                    ),
                )
            if str(result["result_status"]) == "FINAL":
                self._enqueue_suggestion_task(
                    connection,
                    summary_result_id=summary_result_id,
                    revision=revision,
                    source=payload,
                    created_at_ns=closed_at_ns,
                )
            return payload

    # ------------------------------------------------------------------
    # Cloud arbitration write-back
    # ------------------------------------------------------------------

    def apply_arbitration_result(
        self,
        summary_result_id: str,
        arbitration: Mapping[str, Any],
        *,
        now_ns: int,
    ) -> dict[str, Any]:
        """Apply a Cloud arbitration outcome atomically (state + revision + outbox)."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_status, revision, payload_json FROM summary_window_result
                WHERE summary_result_id = ?
                """,
                (str(summary_result_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown summary window: {summary_result_id}")
            payload = json.loads(row["payload_json"])
            arbitration_id = arbitration.get("arbitration_id")
            if row["result_status"] in {"FINAL", "MANUAL_REVIEW"}:
                if payload.get("arbitration_id") == arbitration_id:
                    return payload
                raise ValueError(
                    f"summary window {summary_result_id} is not pending arbitration"
                )
            if row["result_status"] != "PENDING_ARBITRATION":
                raise ValueError(
                    f"summary window {summary_result_id} is not pending arbitration"
                )

            status = str(arbitration.get("status", "")).strip().lower()
            raw_final_state = arbitration.get("final_state")
            final_state = (
                str(raw_final_state).strip().lower()
                if isinstance(raw_final_state, str)
                else None
            )
            final_action = arbitration.get("final_action")
            if status == "resolved" and final_state in BINARY_BEARING_STATES:
                new_status = "FINAL"
                arbitration_status = "RESOLVED"
            elif status == "manual_review" or (
                status == "resolved" and final_state in {"warning", "unknown"}
            ):
                new_status = "MANUAL_REVIEW"
                arbitration_status = "MANUAL_REVIEW"
            else:
                raise ValueError(
                    f"unsupported arbitration outcome: status={status}, final_state={final_state}"
                )

            revision = int(row["revision"]) + 1
            confidence = arbitration.get("confidence")
            payload.update(
                {
                    "result_status": new_status,
                    "arbitration_status": arbitration_status,
                    "arbitration_id": arbitration_id,
                    "final_state": final_state if new_status == "FINAL" else None,
                    "final_action": str(final_action) if final_action else None,
                    "final_source": "cloud_arbitration",
                    "arbitration_confidence": (
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        and 0.0 <= float(confidence) <= 1.0
                        else None
                    ),
                    "revision": revision,
                    "arbitrated_at_ns": int(now_ns),
                }
            )
            if new_status == "FINAL":
                if final_action in GRADE_BY_ACTION:
                    payload["final_action_grade"] = GRADE_BY_ACTION[final_action]
                    payload["recommended_action"] = final_action
                    payload["final_action_level"] = ACTION_TO_LEVEL.get(final_action)
                if payload["arbitration_confidence"] is not None:
                    payload["confidence"] = payload["arbitration_confidence"]
            payload_json = canonical_json(payload)
            connection.execute(
                """
                UPDATE summary_window_result
                SET result_status = ?, revision = ?, payload_json = ?,
                    created_at_ns = ?
                WHERE summary_result_id = ?
                """,
                (new_status, revision, payload_json, int(now_ns), str(summary_result_id)),
            )
            self._insert_delivery(
                connection,
                table="summary_window_publish_outbox",
                request_id=stable_id("publish", summary_result_id, revision),
                summary_result_id=summary_result_id,
                revision=revision,
                payload=payload,
                created_at_ns=int(now_ns),
            )
            self._insert_delivery(
                connection,
                table="summary_window_sync_outbox",
                request_id=stable_id("sync", summary_result_id, revision),
                summary_result_id=summary_result_id,
                revision=revision,
                payload=_sync_projection(payload),
                created_at_ns=int(now_ns),
            )
            if new_status == "FINAL":
                self._enqueue_suggestion_task(
                    connection,
                    summary_result_id=summary_result_id,
                    revision=revision,
                    source=payload,
                    created_at_ns=int(now_ns),
                )
                _increment_counter(connection, "arbitration_resolved_windows")
            else:
                _increment_counter(connection, "arbitration_manual_review_windows")
            return payload

    # ------------------------------------------------------------------
    # Suggestion tasks (async generation)
    # ------------------------------------------------------------------

    def due_suggestion_tasks(self, *, now_ns: int, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
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

    def complete_suggestion_task(
        self,
        summary_result_id: str,
        suggestion: Mapping[str, Any],
        *,
        now_ns: int,
    ) -> None:
        with self._lock, self._connect() as connection:
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

    def defer_suggestion_task(
        self,
        summary_result_id: str,
        *,
        error: str,
        attempts: int,
        next_attempt_at_ns: int,
        dead_letter: bool,
        now_ns: int,
    ) -> None:
        with self._lock, self._connect() as connection:
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

    def get_suggestion(self, summary_result_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM summary_suggestion_outbox WHERE summary_result_id = ? ORDER BY revision DESC LIMIT 1",
                (str(summary_result_id),),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def increment_metric(self, metric: str, amount: int = 1) -> None:
        with self._lock, self._connect() as connection:
            _increment_counter(connection, metric, amount)

    def metrics(self, *, device_id: str | None = None) -> dict[str, Any]:
        where = "WHERE device_id = ?" if device_id else ""
        params: tuple[Any, ...] = (device_id,) if device_id else ()
        with self._lock, self._connect() as connection:
            window = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 0 THEN 1 ELSE 0 END) AS eligible_windows,
                    SUM(CASE WHEN has_conflict = 1 AND excluded_from_formal_metrics = 0 THEN 1 ELSE 0 END) AS conflict_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 1 THEN 1 ELSE 0 END) AS incomplete_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 0
                        AND json_extract(payload_json, '$.node_states.edge_01') = 'normal'
                        AND json_extract(payload_json, '$.node_states.edge_02') = 'normal'
                        THEN 1 ELSE 0 END) AS normal_normal_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 0
                        AND json_extract(payload_json, '$.node_states.edge_01') = 'fault'
                        AND json_extract(payload_json, '$.node_states.edge_02') = 'fault'
                        THEN 1 ELSE 0 END) AS fault_fault_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 0
                        AND json_extract(payload_json, '$.node_states.edge_01') = 'normal'
                        AND json_extract(payload_json, '$.node_states.edge_02') = 'fault'
                        THEN 1 ELSE 0 END) AS normal_fault_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 0
                        AND json_extract(payload_json, '$.node_states.edge_01') = 'fault'
                        AND json_extract(payload_json, '$.node_states.edge_02') = 'normal'
                        THEN 1 ELSE 0 END) AS fault_normal_windows,
                    SUM(CASE WHEN excluded_from_formal_metrics = 1
                        AND json_extract(payload_json, '$.incomplete_reason') = 'INSUFFICIENT_EDGE_DIVERSITY'
                        THEN 1 ELSE 0 END) AS same_edge_windows,
                    SUM(COALESCE(json_array_length(payload_json, '$.missing_bearing_ids'), 0)) AS missing_node_count,
                    SUM(CASE WHEN json_array_length(payload_json, '$.missing_bearing_ids') > 0 THEN 1 ELSE 0 END) AS missing_node_windows,
                    AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_grade_gap') END) AS average_decision_gap,
                    MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_grade_gap') END) AS maximum_decision_gap,
                    AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_level_gap') END) AS average_action_level_gap,
                    MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_level_gap') END) AS maximum_action_level_gap,
                    AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_score_gap') END) AS average_action_score_gap,
                    MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_score_gap') END) AS maximum_action_score_gap,
                    AVG(json_extract(payload_json, '$.window_close_duration_ns')) AS average_window_close_ns
                FROM summary_window_result
                {where}
                """,
                params,
            ).fetchone()
            semantics_rows = connection.execute(
                f"""
                SELECT
                    COALESCE(json_extract(payload_json, '$.conflict_semantics'), 'legacy') AS semantics,
                    COUNT(*) AS count
                FROM summary_window_result
                WHERE excluded_from_formal_metrics = 0
                {("AND device_id = ?" if device_id else "")}
                GROUP BY semantics
                """,
                params,
            ).fetchall()
            outbox_where = "WHERE window.device_id = ?" if device_id else ""
            outbox = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS upload_windows,
                    SUM(CASE WHEN outbox.state = 'ACKNOWLEDGED' THEN 1 ELSE 0 END) AS acknowledged_windows,
                    SUM(CASE WHEN outbox.state IN ('PENDING', 'UPLOADING', 'RETRY_WAIT') THEN 1 ELSE 0 END) AS pending_windows,
                    SUM(CASE WHEN outbox.state = 'DEAD_LETTER' THEN 1 ELSE 0 END) AS dead_letter_windows
                FROM summary_arbitration_outbox AS outbox
                JOIN summary_window_result AS window
                  ON window.summary_result_id = outbox.summary_result_id
                {outbox_where}
                """,
                params,
            ).fetchone()
            counters = {
                row["metric"]: int(row["value"])
                for row in connection.execute(
                    "SELECT metric, value FROM summary_metrics_counter"
                ).fetchall()
            }
            outbox_backlog: dict[str, dict[str, int]] = {}
            for table in _OUTBOX_TABLES:
                rows = connection.execute(
                    f"SELECT state, COUNT(*) AS count FROM {table} GROUP BY state"
                ).fetchall()
                counts = {row["state"]: int(row["count"]) for row in rows}
                outbox_backlog[table] = {
                    "pending": counts.get("PENDING", 0)
                    + counts.get("UPLOADING", 0)
                    + counts.get("RETRY_WAIT", 0),
                    "acknowledged": counts.get("ACKNOWLEDGED", 0),
                    "dead_letter": counts.get("DEAD_LETTER", 0),
                }
            suggestion_tasks = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM summary_suggestion_task GROUP BY state"
                ).fetchall()
            }

        total = int(window["total_windows"] or 0)
        eligible = int(window["eligible_windows"] or 0)
        conflicts = int(window["conflict_windows"] or 0)
        incomplete = int(window["incomplete_windows"] or 0)
        uploads = int(outbox["upload_windows"] or 0)
        acknowledged = int(outbox["acknowledged_windows"] or 0)
        pending = int(outbox["pending_windows"] or 0)
        dead_letter = int(outbox["dead_letter_windows"] or 0)
        return {
            "total_windows": total,
            "eligible_windows": eligible,
            "conflict_windows": conflicts,
            "incomplete_windows": incomplete,
            "conflict_rate": conflicts / eligible if eligible else 0.0,
            "consistency_rate": (eligible - conflicts) / eligible if eligible else 0.0,
            "state_combinations": {
                "normal_normal": int(window["normal_normal_windows"] or 0),
                "fault_fault": int(window["fault_fault_windows"] or 0),
                "normal_fault": int(window["normal_fault_windows"] or 0),
                "fault_normal": int(window["fault_normal_windows"] or 0),
            },
            "same_edge_windows": int(window["same_edge_windows"] or 0),
            "missing_node_windows": int(window["missing_node_windows"] or 0),
            "missing_node_count": int(window["missing_node_count"] or 0),
            "average_window_close_ns": (
                float(window["average_window_close_ns"])
                if window["average_window_close_ns"] is not None
                else None
            ),
            "average_decision_gap": (
                float(window["average_decision_gap"])
                if window["average_decision_gap"] is not None
                else None
            ),
            "maximum_decision_gap": (
                int(window["maximum_decision_gap"])
                if window["maximum_decision_gap"] is not None
                else None
            ),
            "average_action_level_gap": (
                float(window["average_action_level_gap"])
                if window["average_action_level_gap"] is not None
                else None
            ),
            "maximum_action_level_gap": (
                int(window["maximum_action_level_gap"])
                if window["maximum_action_level_gap"] is not None
                else None
            ),
            "average_action_score_gap": (
                float(window["average_action_score_gap"])
                if window["average_action_score_gap"] is not None
                else None
            ),
            "maximum_action_score_gap": (
                float(window["maximum_action_score_gap"])
                if window["maximum_action_score_gap"] is not None
                else None
            ),
            "conflict_semantics_distribution": {
                str(row["semantics"]): int(row["count"])
                for row in semantics_rows
            },
            "arbitration_upload_windows": uploads,
            "arbitration_acknowledged_windows": acknowledged,
            "arbitration_pending_windows": pending,
            "arbitration_dead_letter_windows": dead_letter,
            "arbitration_upload_success_rate": acknowledged / conflicts if conflicts else 0.0,
            "counters": counters,
            "outbox_backlog": outbox_backlog,
            "suggestion_tasks": {
                "pending": suggestion_tasks.get("PENDING", 0)
                + suggestion_tasks.get("RETRY_WAIT", 0),
                "completed": suggestion_tasks.get("COMPLETED", 0),
                "dead_letter": suggestion_tasks.get("DEAD_LETTER", 0),
            },
        }

    def list_window_results(
        self, *, device_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "WHERE device_id = ?" if device_id else ""
        params: tuple[Any, ...] = (device_id, int(limit)) if device_id else (int(limit),)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM summary_window_result
                {where}
                ORDER BY created_at_ns DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _insert_delivery(
        connection: sqlite3.Connection,
        *,
        table: str,
        request_id: str,
        summary_result_id: str,
        revision: int,
        payload: Mapping[str, Any],
        created_at_ns: int,
    ) -> None:
        if table not in {"summary_window_publish_outbox", "summary_window_sync_outbox"}:
            raise ValueError("unsupported delivery table")
        connection.execute(
            f"""
            INSERT OR IGNORE INTO {table} (
                request_id, summary_result_id, revision, payload_json,
                state, attempts, next_attempt_at_ns, created_at_ns
            ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, ?)
            """,
            (
                request_id,
                summary_result_id,
                int(revision),
                canonical_json(payload),
                int(created_at_ns),
            ),
        )

    @staticmethod
    def _enqueue_suggestion_task(
        connection: sqlite3.Connection,
        *,
        summary_result_id: str,
        revision: int,
        source: Mapping[str, Any],
        created_at_ns: int,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO summary_suggestion_task (
                summary_result_id, revision, source_json, state, attempts,
                next_attempt_at_ns, created_at_ns, updated_at_ns
            ) VALUES (?, ?, ?, 'PENDING', 0, 0, ?, ?)
            """,
            (
                str(summary_result_id),
                int(revision),
                canonical_json(source),
                int(created_at_ns),
                int(created_at_ns),
            ),
        )


def _sync_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "summary_result_id": str(payload["summary_result_id"]),
        "summary_window_id": str(payload["summary_window_id"]),
        "device_id": str(payload["device_id"]),
        "run_id": payload.get("run_id"),
        "window_start_sequence": int(payload["window_start_sequence"]),
        "window_end_sequence": int(payload["window_end_sequence"]),
        "result_status": str(payload["result_status"]),
        "revision": int(payload["revision"]),
        "has_conflict": bool(payload["has_conflict"]),
        "conflict_semantics": str(payload.get("conflict_semantics", CONFLICT_SEMANTICS)),
        "action_scorer_version": str(
            payload.get("action_scorer_version", ACTION_SCORER_VERSION)
        ),
        "state_mismatch": bool(payload.get("state_mismatch", False)),
        "state_mismatch_pair_count": int(payload.get("state_mismatch_pair_count", 0)),
        "node_states": dict(payload.get("node_states", {})),
        "final_state": payload.get("final_state"),
        "arbitration_status": payload.get("arbitration_status"),
        "excluded_from_formal_metrics": bool(payload["excluded_from_formal_metrics"]),
        "max_cross_edge_grade_gap": int(payload.get("max_grade_gap", 0)),
        "max_action_level_gap": int(payload.get("max_action_level_gap", 0)),
        "max_action_score_gap": float(payload.get("max_action_score_gap", 0.0)),
        "max_observed_action_level": payload.get("max_observed_action_level"),
        "max_observed_action_score": payload.get("max_observed_action_score"),
        "action_levels_by_edge": dict(payload.get("action_levels_by_edge", {})),
        "action_scores_by_edge": dict(payload.get("action_scores_by_edge", {})),
        "final_action_level": payload.get("final_action_level"),
        "final_action_grade": payload.get("final_action_grade"),
        "recommended_action": payload.get("recommended_action"),
        "conflicting_pair_count": int(payload.get("conflict_pair_count", 0)),
        "closed_at_ns": int(payload["closed_at_ns"]),
    }


def _increment_counter(
    connection: sqlite3.Connection, metric: str, amount: int = 1
) -> None:
    connection.execute(
        """
        INSERT INTO summary_metrics_counter(metric, value) VALUES (?, ?)
        ON CONFLICT(metric) DO UPDATE SET value = value + excluded.value
        """,
        (metric, int(amount)),
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Rebuild the v1 schema with node-dimension identity (payload preserved)."""

    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _table_exists(connection, "summary_bearing_result") and (
            "summary_window_id" not in _columns(connection, "summary_bearing_result")
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

        if _table_exists(connection, "summary_window_result") and (
            "summary_window_id" not in _columns(connection, "summary_window_result")
        ):
            connection.execute(
                "ALTER TABLE summary_window_result RENAME TO summary_window_result_legacy_v1"
            )
            connection.executescript(SCHEMA_V2)
            rows = connection.execute(
                "SELECT * FROM summary_window_result_legacy_v1"
            ).fetchall()
            columns = set(rows[0].keys()) if rows else set()
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
                        row["has_conflict"] if "has_conflict" in columns else 0,
                        (
                            row["excluded_from_formal_metrics"]
                            if "excluded_from_formal_metrics" in columns
                            else 0
                        ),
                        row["payload_json"],
                        row["created_at_ns"],
                    ),
                )
            connection.execute("DROP TABLE summary_window_result_legacy_v1")

        _rebuild_outbox_tables(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _rebuild_outbox_tables(connection: sqlite3.Connection) -> None:
    for table in _OUTBOX_TABLES:
        if not _table_exists(connection, table):
            continue
        if "revision" in _columns(connection, table):
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


def _outbox_values(row: sqlite3.Row, *, with_conflict_id: bool) -> tuple[Any, ...]:
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
