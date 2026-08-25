from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .aggregation import build_arbitration_request
from .contracts import canonical_json, stable_id


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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS summary_bearing_result (
                    result_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    window_start_sequence INTEGER NOT NULL,
                    window_end_sequence INTEGER NOT NULL,
                    bearing_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at_ns INTEGER NOT NULL,
                    UNIQUE(device_id, window_start_sequence, window_end_sequence, bearing_id)
                );

                CREATE TABLE IF NOT EXISTS summary_window_result (
                    summary_result_id TEXT PRIMARY KEY,
                    conflict_id TEXT,
                    device_id TEXT NOT NULL,
                    window_start_sequence INTEGER NOT NULL,
                    window_end_sequence INTEGER NOT NULL,
                    result_status TEXT NOT NULL,
                    has_conflict INTEGER NOT NULL,
                    excluded_from_formal_metrics INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    UNIQUE(device_id, window_start_sequence, window_end_sequence)
                );

                CREATE TABLE IF NOT EXISTS summary_arbitration_outbox (
                    request_id TEXT PRIMARY KEY,
                    conflict_id TEXT NOT NULL UNIQUE,
                    summary_result_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at_ns INTEGER NOT NULL,
                    acknowledged_at_ns INTEGER,
                    cloud_result_json TEXT,
                    FOREIGN KEY(summary_result_id) REFERENCES summary_window_result(summary_result_id)
                );

                CREATE TABLE IF NOT EXISTS summary_window_publish_outbox (
                    request_id TEXT PRIMARY KEY,
                    conflict_id TEXT NOT NULL UNIQUE,
                    summary_result_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at_ns INTEGER NOT NULL,
                    acknowledged_at_ns INTEGER,
                    cloud_result_json TEXT
                );

                CREATE TABLE IF NOT EXISTS summary_window_sync_outbox (
                    request_id TEXT PRIMARY KEY,
                    conflict_id TEXT NOT NULL UNIQUE,
                    summary_result_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at_ns INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at_ns INTEGER NOT NULL,
                    acknowledged_at_ns INTEGER,
                    cloud_result_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_summary_bearing_group
                    ON summary_bearing_result(device_id, window_start_sequence, window_end_sequence);
                CREATE INDEX IF NOT EXISTS idx_summary_window_device
                    ON summary_window_result(device_id, created_at_ns DESC);
                CREATE INDEX IF NOT EXISTS idx_summary_outbox_due
                    ON summary_arbitration_outbox(state, next_attempt_at_ns);
                CREATE INDEX IF NOT EXISTS idx_summary_publish_due
                    ON summary_window_publish_outbox(state, next_attempt_at_ns);
                CREATE INDEX IF NOT EXISTS idx_summary_sync_due
                    ON summary_window_sync_outbox(state, next_attempt_at_ns);
                """
            )

    def save_bearing_result(
        self, result: Mapping[str, Any], *, received_at_ns: int
    ) -> bool:
        values = (
            result["result_id"],
            result["device_id"],
            int(result["window_start_sequence"]),
            int(result["window_end_sequence"]),
            result["bearing_id"],
            canonical_json(result),
            int(received_at_ns),
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO summary_bearing_result (
                        result_id, device_id, window_start_sequence, window_end_sequence,
                        bearing_id, payload_json, received_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return True
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT result_id, payload_json
                    FROM summary_bearing_result
                    WHERE result_id = ? OR (
                        device_id = ? AND window_start_sequence = ?
                        AND window_end_sequence = ? AND bearing_id = ?
                    )
                    """,
                    (values[0], values[1], values[2], values[3], values[4]),
                ).fetchone()
                if existing is not None and existing["payload_json"] == values[5]:
                    return False
                raise ValueError("bearing result identity conflicts with an existing result") from None

    def load_window_bearing_results(
        self, device_id: str, window_start_sequence: int, window_end_sequence: int
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM summary_bearing_result
                WHERE device_id = ? AND window_start_sequence = ? AND window_end_sequence = ?
                ORDER BY bearing_id
                """,
                (device_id, int(window_start_sequence), int(window_end_sequence)),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_window_result(
        self, device_id: str, window_start_sequence: int, window_end_sequence: int
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM summary_window_result
                WHERE device_id = ? AND window_start_sequence = ? AND window_end_sequence = ?
                """,
                (device_id, int(window_start_sequence), int(window_end_sequence)),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def load_expired_open_windows(self, *, cutoff_ns: int) -> list[list[dict[str, Any]]]:
        with self._lock, self._connect() as connection:
            groups = connection.execute(
                """
                SELECT bearing.device_id, bearing.window_start_sequence,
                       bearing.window_end_sequence
                FROM summary_bearing_result AS bearing
                LEFT JOIN summary_window_result AS window
                  ON window.device_id = bearing.device_id
                 AND window.window_start_sequence = bearing.window_start_sequence
                 AND window.window_end_sequence = bearing.window_end_sequence
                WHERE window.summary_result_id IS NULL
                GROUP BY bearing.device_id, bearing.window_start_sequence,
                         bearing.window_end_sequence
                HAVING MIN(bearing.received_at_ns) <= ?
                """,
                (int(cutoff_ns),),
            ).fetchall()
        return [
            self.load_window_bearing_results(
                row["device_id"],
                row["window_start_sequence"],
                row["window_end_sequence"],
            )
            for row in groups
        ]

    def save_window_result(self, result: Mapping[str, Any]) -> bool:
        arbitration_request = (
            build_arbitration_request(result) if result.get("has_conflict") else None
        )
        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO summary_window_result (
                        summary_result_id, conflict_id, device_id,
                        window_start_sequence, window_end_sequence, result_status,
                        has_conflict, excluded_from_formal_metrics, payload_json, created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result["summary_result_id"],
                        result.get("conflict_id"),
                        result["device_id"],
                        int(result["window_start_sequence"]),
                        int(result["window_end_sequence"]),
                        result["result_status"],
                        int(bool(result["has_conflict"])),
                        int(bool(result["excluded_from_formal_metrics"])),
                        canonical_json(result),
                        int(result["closed_at_ns"]),
                    ),
                )
                if arbitration_request is not None:
                    request_id = stable_id(
                        "arbitration", arbitration_request["conflict_id"]
                    )
                    connection.execute(
                        """
                        INSERT INTO summary_arbitration_outbox (
                            request_id, conflict_id, summary_result_id, payload_json,
                            state, attempts, next_attempt_at_ns, created_at_ns
                        ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, ?)
                        """,
                        (
                            request_id,
                            arbitration_request["conflict_id"],
                            arbitration_request["summary_result_id"],
                            canonical_json(arbitration_request),
                            int(result["closed_at_ns"]),
                        ),
                    )
                self._insert_delivery(
                    connection,
                    table="summary_window_publish_outbox",
                    request_id=stable_id("publish", result["summary_result_id"]),
                    identity=result["summary_result_id"],
                    summary_result_id=result["summary_result_id"],
                    payload=result,
                    created_at_ns=int(result["closed_at_ns"]),
                )
                self._insert_delivery(
                    connection,
                    table="summary_window_sync_outbox",
                    request_id=stable_id("sync", result["summary_result_id"]),
                    identity=result["summary_result_id"],
                    summary_result_id=result["summary_result_id"],
                    payload={
                        "summary_result_id": result["summary_result_id"],
                        "device_id": result["device_id"],
                        "window_start_sequence": int(result["window_start_sequence"]),
                        "window_end_sequence": int(result["window_end_sequence"]),
                        "result_status": result["result_status"],
                        "has_conflict": bool(result["has_conflict"]),
                        "excluded_from_formal_metrics": bool(
                            result["excluded_from_formal_metrics"]
                        ),
                        "max_cross_edge_grade_gap": int(result["max_grade_gap"]),
                        "conflicting_pair_count": int(result["conflict_pair_count"]),
                        "closed_at_ns": int(result["closed_at_ns"]),
                    },
                    created_at_ns=int(result["closed_at_ns"]),
                )
                return cursor.rowcount == 1
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT payload_json FROM summary_window_result
                    WHERE summary_result_id = ? OR (
                        device_id = ? AND window_start_sequence = ? AND window_end_sequence = ?
                    )
                    """,
                    (
                        result["summary_result_id"],
                        result["device_id"],
                        int(result["window_start_sequence"]),
                        int(result["window_end_sequence"]),
                    ),
                ).fetchone()
                if existing is not None and existing["payload_json"] == canonical_json(result):
                    return False
                raise ValueError("window result conflicts with an existing result") from None

    @staticmethod
    def _insert_delivery(
        connection: sqlite3.Connection,
        *,
        table: str,
        request_id: str,
        identity: str,
        summary_result_id: str,
        payload: Mapping[str, Any],
        created_at_ns: int,
    ) -> None:
        if table not in {
            "summary_window_publish_outbox",
            "summary_window_sync_outbox",
        }:
            raise ValueError("unsupported delivery table")
        connection.execute(
            f"""
            INSERT INTO {table} (
                request_id, conflict_id, summary_result_id, payload_json,
                state, attempts, next_attempt_at_ns, created_at_ns
            ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, ?)
            """,
            (
                request_id,
                identity,
                summary_result_id,
                canonical_json(payload),
                created_at_ns,
            ),
        )

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
                    AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_grade_gap') END) AS average_decision_gap,
                    MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_grade_gap') END) AS maximum_decision_gap
                FROM summary_window_result
                {where}
                """,
                params,
            ).fetchone()
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
            "arbitration_upload_windows": uploads,
            "arbitration_acknowledged_windows": acknowledged,
            "arbitration_pending_windows": pending,
            "arbitration_dead_letter_windows": dead_letter,
            "arbitration_upload_success_rate": acknowledged / conflicts if conflicts else 0.0,
        }
