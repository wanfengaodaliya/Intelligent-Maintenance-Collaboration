from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import canonical_json
from .ports import BearingResultConflictError
from .storage.schema import (
    SCHEMA_V2,
    SCHEMA_VERSION,
    columns as _columns,
    migrate_v1_to_v2 as _migrate_v1_to_v2,
    table_exists as _table_exists,
)
from .storage.metrics import increment_counter, load_metrics
from .storage.suggestions import (
    complete_task as complete_suggestion_task,
    defer_task as defer_suggestion_task,
    due_tasks as due_suggestion_tasks,
    get_suggestion as load_suggestion,
)
from .storage.windows import (
    apply_arbitration_result as apply_arbitration_result_transaction,
    save_window_result as save_window_result_transaction,
)
from .sync_contract import build_summary_window_sync_payload as _sync_projection


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
        with self._lock, self._connect() as connection:
            return save_window_result_transaction(connection, result)

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
        with self._lock, self._connect() as connection:
            return apply_arbitration_result_transaction(
                connection,
                summary_result_id,
                arbitration,
                now_ns=now_ns,
            )

    # ------------------------------------------------------------------
    # Suggestion tasks (async generation)
    # ------------------------------------------------------------------

    def due_suggestion_tasks(self, *, now_ns: int, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            return due_suggestion_tasks(connection, now_ns=now_ns, limit=limit)

    def complete_suggestion_task(
        self,
        summary_result_id: str,
        suggestion: Mapping[str, Any],
        *,
        now_ns: int,
    ) -> None:
        with self._lock, self._connect() as connection:
            complete_suggestion_task(
                connection,
                summary_result_id,
                suggestion,
                now_ns=now_ns,
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
            defer_suggestion_task(
                connection,
                summary_result_id,
                error=error,
                attempts=attempts,
                next_attempt_at_ns=next_attempt_at_ns,
                dead_letter=dead_letter,
                now_ns=now_ns,
            )

    def get_suggestion(self, summary_result_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            return load_suggestion(connection, summary_result_id)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def increment_metric(self, metric: str, amount: int = 1) -> None:
        with self._lock, self._connect() as connection:
            increment_counter(connection, metric, amount)

    def metrics(self, *, device_id: str | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            return load_metrics(connection, device_id=device_id)

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
