"""SQLite storage for task assignments, attempts, and execution results."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


class TaskRepositoryError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TaskRepository:
    def __init__(
        self,
        database_path: Path | str | None = None,
        *,
        sqlite_timeout_seconds: float | None = None,
    ) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "scheduler.db"
        self.database_path = Path(
            database_path or os.getenv("SCHEDULER_DB_PATH", str(default_path))
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_timeout_seconds = float(
            sqlite_timeout_seconds
            if sqlite_timeout_seconds is not None
            else os.getenv("SCHEDULER_SQLITE_TIMEOUT_SECONDS", "0.1")
        )
        if self.sqlite_timeout_seconds <= 0:
            raise ValueError("sqlite_timeout_seconds must be positive")
        self._initialize()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_assignment WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def retry_constraints(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT edge_node_id,ack_status FROM assignment_attempt "
                "WHERE task_id=? ORDER BY attempt_number",
                (task_id,),
            ).fetchall()
        rejected_edge_node_ids = sorted(
            {
                row["edge_node_id"]
                for row in rows
                if row["ack_status"] == "REJECTED"
            }
        )
        pinned_edge_node_id = None
        if rows and rows[-1]["ack_status"] in {"FAILED", "PENDING"}:
            pinned_edge_node_id = rows[-1]["edge_node_id"]
        return {
            "rejected_edge_node_ids": rejected_edge_node_ids,
            "pinned_edge_node_id": pinned_edge_node_id,
        }

    def claim(
        self,
        request: Mapping[str, Any],
        claim_id: str,
        *,
        lease_seconds: float = 5.0,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        now = now_ns or time.time_ns()
        lease_expires_at_ns = now + int(lease_seconds * 1_000_000_000)
        bearings_json = json.dumps(
            [
                {
                    "bearing_id": request["bearing_id"],
                    "packet_size_bytes": request["packet_size_bytes"],
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO task_assignment("
                "task_id,device_id,sender_id,bearing_id,bearings_json,packet_size_bytes,"
                "expected_packet_count,expected_duration_ms,created_timestamp_ns,"
                "assignment_status,attempt_count,scheduling_owner,lease_expires_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,?,?,?,'SCHEDULING',0,?,?,?)",
                (
                    request["task_id"],
                    request["device_id"],
                    request["sender_id"],
                    request["bearing_id"],
                    bearings_json,
                    request["packet_size_bytes"],
                    request["expected_packet_count"],
                    request["expected_duration_ms"],
                    request["created_timestamp_ns"],
                    claim_id,
                    lease_expires_at_ns,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM task_assignment WHERE task_id=?",
                (request["task_id"],),
            ).fetchone()
            if row is None:
                raise RuntimeError("task row was not created")
            task = dict(row)
            expected_values = {
                "device_id": request["device_id"],
                "sender_id": request["sender_id"],
                "bearing_id": request["bearing_id"],
                "bearings_json": bearings_json,
                "packet_size_bytes": request["packet_size_bytes"],
                "expected_packet_count": request["expected_packet_count"],
                "expected_duration_ms": request["expected_duration_ms"],
                "created_timestamp_ns": request["created_timestamp_ns"],
            }
            for field, expected in expected_values.items():
                if task[field] != expected:
                    raise TaskRepositoryError(
                        "TASK_ID_CONFLICT",
                        f"task_id already exists with different {field}",
                        409,
                    )
            if task["assignment_status"] == "ASSIGNED":
                return task
            if (
                task["assignment_status"] == "SCHEDULING"
                and task["scheduling_owner"] not in {None, claim_id}
                and int(task["lease_expires_at_ns"] or 0) > now
            ):
                raise TaskRepositoryError(
                    "TASK_SCHEDULING",
                    "task assignment is already in progress",
                    409,
                )
            connection.execute(
                "UPDATE task_assignment SET assignment_status='SCHEDULING',"
                "scheduling_owner=?,lease_expires_at_ns=?,failure_code=NULL,updated_at_ns=? "
                "WHERE task_id=?",
                (claim_id, lease_expires_at_ns, now, request["task_id"]),
            )
            task["assignment_status"] = "SCHEDULING"
            task["scheduling_owner"] = claim_id
            task["lease_expires_at_ns"] = lease_expires_at_ns
            task["failure_code"] = None
        return task

    def start_attempt(
        self,
        task_id: str,
        edge_node_id: str,
        claim_id: str,
        *,
        bearing_id: str | None = None,
        started_at_ns: int | None = None,
    ) -> tuple[int, int]:
        now = started_at_ns or time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt_count,assignment_status,scheduling_owner "
                "FROM task_assignment WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TaskRepositoryError("TASK_NOT_FOUND", "task does not exist", 404)
            if row["assignment_status"] != "SCHEDULING" or row["scheduling_owner"] != claim_id:
                raise TaskRepositoryError("TASK_CLAIM_LOST", "task scheduling claim was lost", 409)
            attempt_number = int(row["attempt_count"]) + 1
            cursor = connection.execute(
                "UPDATE task_assignment SET attempt_count=?,updated_at_ns=? "
                "WHERE task_id=? AND assignment_status='SCHEDULING' "
                "AND scheduling_owner=?",
                (attempt_number, now, task_id, claim_id),
            )
            if cursor.rowcount != 1:
                raise TaskRepositoryError(
                    "TASK_CLAIM_LOST", "task scheduling claim was lost", 409
                )
            attempt_cursor = connection.execute(
                "INSERT INTO assignment_attempt("
                "task_id,bearing_id,edge_node_id,attempt_number,ack_status,started_at_ns"
                ") VALUES (?,?,?,?,'PENDING',?)",
                (task_id, bearing_id, edge_node_id, attempt_number, now),
            )
        return int(attempt_cursor.lastrowid), attempt_number

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        ack_status: str,
        reason_code: str | None,
        finished_at_ns: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE assignment_attempt SET ack_status=?,reason_code=?,finished_at_ns=? "
                "WHERE id=?",
                (ack_status, reason_code, finished_at_ns or time.time_ns(), attempt_id),
            )

    def fail_attempt(
        self,
        attempt_id: int,
        task_id: str,
        claim_id: str,
        failure_code: str,
        *,
        attempt_status: str = "FAILED",
        finished_at_ns: int | None = None,
    ) -> int:
        if attempt_status not in {"FAILED", "REJECTED"}:
            raise ValueError("attempt_status must be FAILED or REJECTED")
        now = finished_at_ns or time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT task_id FROM assignment_attempt WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["task_id"] != task_id:
                raise TaskRepositoryError(
                    "INVALID_ASSIGNMENT_ATTEMPT",
                    "assignment attempt does not match the failed task",
                    409,
                )
            task_cursor = connection.execute(
                "UPDATE task_assignment SET assignment_status='FAILED',failure_code=?,"
                "scheduling_owner=NULL,lease_expires_at_ns=NULL,updated_at_ns=? "
                "WHERE task_id=? AND assignment_status='SCHEDULING' "
                "AND scheduling_owner=?",
                (failure_code, now, task_id, claim_id),
            )
            if task_cursor.rowcount != 1:
                raise TaskRepositoryError(
                    "TASK_CLAIM_LOST",
                    "task scheduling claim was lost",
                    409,
                )
            connection.execute(
                "UPDATE assignment_attempt SET ack_status=?,reason_code=?,"
                "finished_at_ns=? WHERE id=?",
                (attempt_status, failure_code, now, attempt_id),
            )
        return now

    def accept_attempt(
        self,
        attempt_id: int,
        task_id: str,
        claim_id: str,
        edge_node_id: str,
        target_topic: str,
        *,
        assigned_at_ns: int | None = None,
    ) -> None:
        now = assigned_at_ns or time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT task_id,edge_node_id,ack_status FROM assignment_attempt WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if (
                attempt is None
                or attempt["task_id"] != task_id
                or attempt["edge_node_id"] != edge_node_id
                or attempt["ack_status"] not in {"PENDING", "ACCEPTED"}
            ):
                raise TaskRepositoryError(
                    "INVALID_ASSIGNMENT_ATTEMPT",
                    "assignment attempt does not match the accepted task",
                    409,
                )
            task = connection.execute(
                "SELECT assignment_status,edge_node_id,target_topic,scheduling_owner "
                "FROM task_assignment WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TaskRepositoryError("TASK_NOT_FOUND", "task does not exist", 404)
            if task["assignment_status"] == "ASSIGNED":
                if task["edge_node_id"] != edge_node_id or task["target_topic"] != target_topic:
                    raise TaskRepositoryError(
                        "TASK_ALREADY_ASSIGNED",
                        "task was already assigned to a different edge node",
                        409,
                    )
            else:
                if (
                    task["assignment_status"] != "SCHEDULING"
                    or task["scheduling_owner"] != claim_id
                ):
                    raise TaskRepositoryError(
                        "TASK_CLAIM_LOST",
                        "task scheduling claim was lost",
                        409,
                    )
                cursor = connection.execute(
                    "UPDATE task_assignment SET assignment_status='ASSIGNED',edge_node_id=?,"
                    "target_topic=?,provisional_assignments_json=NULL,assignments_json=NULL,"
                    "assigned_at_ns=?,failure_code=NULL,scheduling_owner=NULL,"
                    "lease_expires_at_ns=NULL,updated_at_ns=? "
                    "WHERE task_id=? AND assignment_status='SCHEDULING' "
                    "AND scheduling_owner=?",
                    (edge_node_id, target_topic, now, now, task_id, claim_id),
                )
                if cursor.rowcount != 1:
                    raise TaskRepositoryError(
                        "TASK_CLAIM_LOST",
                        "task scheduling claim was lost",
                        409,
                    )
            connection.execute(
                "UPDATE assignment_attempt SET ack_status='ACCEPTED',reason_code=NULL,"
                "finished_at_ns=? WHERE id=?",
                (now, attempt_id),
            )

    def mark_failed(
        self,
        task_id: str,
        failure_code: str,
        claim_id: str,
    ) -> int | None:
        now = time.time_ns()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE task_assignment SET assignment_status='FAILED',failure_code=?,"
                "scheduling_owner=NULL,lease_expires_at_ns=NULL,updated_at_ns=? "
                "WHERE task_id=? AND scheduling_owner=?",
                (failure_code, now, task_id, claim_id),
            )
        return now if cursor.rowcount == 1 else None

    def replace_failure_code(
        self,
        task_id: str,
        previous_updated_at_ns: int,
        failure_code: str,
        *,
        attempt_id: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE task_assignment SET failure_code=?,updated_at_ns=? "
                "WHERE task_id=? AND assignment_status='FAILED' AND updated_at_ns=?",
                (failure_code, time.time_ns(), task_id, previous_updated_at_ns),
            )
            if cursor.rowcount == 1 and attempt_id is not None:
                connection.execute(
                    "UPDATE assignment_attempt SET reason_code=? "
                    "WHERE id=? AND task_id=?",
                    (failure_code, attempt_id, task_id),
                )
        return cursor.rowcount == 1

    def save_result(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        validated = _validate_result(payload)
        now = time.time_ns()
        if validated["completed_at_ns"] > now + 300_000_000_000:
            raise TaskRepositoryError(
                "INVALID_TASK_RESULT",
                "completed_at_ns is too far in the future",
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_assignment WHERE task_id=?",
                (validated["task_id"],),
            ).fetchone()
            if row is None:
                raise TaskRepositoryError("TASK_NOT_FOUND", "task does not exist", 404)
            if row["assignment_status"] != "ASSIGNED":
                raise TaskRepositoryError("TASK_NOT_ASSIGNED", "task has no completed assignment", 409)
            if row["bearing_id"] is not None:
                if validated["bearing_id"] is None:
                    raise TaskRepositoryError(
                        "INVALID_TASK_RESULT",
                        "bearing_id is required for single-bearing tasks",
                    )
                if validated["bearing_id"] != row["bearing_id"]:
                    raise TaskRepositoryError(
                        "BEARING_NOT_ASSIGNED",
                        "result bearing_id does not match the assigned bearing",
                        409,
                    )
            if not row["assignments_json"]:
                if row["edge_node_id"] != validated["edge_node_id"]:
                    raise TaskRepositoryError(
                        "EDGE_NODE_MISMATCH",
                        "result edge_node_id does not match the assigned node",
                        409,
                    )
                if row["execution_status"] is not None:
                    same_result = all(
                        row[field] == validated[field]
                        for field in (
                            "execution_status",
                            "processed_packet_count",
                            "processing_latency_ms",
                            "completed_at_ns",
                        )
                    ) and row["result_reason_code"] == validated["reason_code"]
                    if not same_result:
                        raise TaskRepositoryError(
                            "TASK_RESULT_CONFLICT",
                            "task already has a different execution result",
                            409,
                        )
                    return {
                        "task_id": validated["task_id"],
                        "edge_node_id": validated["edge_node_id"],
                        "saved": True,
                        "duplicate": True,
                    }
                connection.execute(
                    "UPDATE task_assignment SET execution_status=?,processed_packet_count=?,"
                    "processing_latency_ms=?,completed_at_ns=?,result_reason_code=?,"
                    "result_received_at_ns=?,updated_at_ns=? WHERE task_id=?",
                    (
                        validated["execution_status"],
                        validated["processed_packet_count"],
                        validated["processing_latency_ms"],
                        validated["completed_at_ns"],
                        validated["reason_code"],
                        now,
                        now,
                        validated["task_id"],
                    ),
                )
                return {
                    "task_id": validated["task_id"],
                    "edge_node_id": validated["edge_node_id"],
                    "saved": True,
                    "duplicate": False,
                }
            try:
                assignments = json.loads(row["assignments_json"] or "[]")
            except json.JSONDecodeError as exc:
                raise TaskRepositoryError(
                    "INVALID_STORED_ASSIGNMENT",
                    "stored task assignment is invalid",
                    500,
                ) from exc
            if validated["bearing_id"] is None:
                assignment = next(
                    (
                        item
                        for item in assignments
                        if item.get("edge_node_id") == validated["edge_node_id"]
                    ),
                    None,
                )
            else:
                assignment = next(
                    (
                        item
                        for item in assignments
                        if item.get("bearing_id") == validated["bearing_id"]
                    ),
                    None,
                )
            if assignment is None:
                raise TaskRepositoryError(
                    "BEARING_NOT_ASSIGNED",
                    "result does not match an assigned bearing",
                    409,
                )
            if assignment.get("edge_node_id") != validated["edge_node_id"]:
                raise TaskRepositoryError(
                    "EDGE_NODE_MISMATCH",
                    "result edge_node_id does not match the bearing assignment",
                    409,
                )
            validated["bearing_id"] = assignment["bearing_id"]
            result = connection.execute(
                "SELECT * FROM bearing_execution_result WHERE task_id=? AND bearing_id=?",
                (validated["task_id"], validated["bearing_id"]),
            ).fetchone()
            if result is not None:
                same_result = all(
                    result[field] == validated[field]
                    for field in (
                        "execution_status",
                        "processed_packet_count",
                        "processing_latency_ms",
                        "completed_at_ns",
                    )
                ) and result["reason_code"] == validated["reason_code"]
                if not same_result:
                    raise TaskRepositoryError(
                        "TASK_RESULT_CONFLICT",
                        "task already has a different execution result",
                        409,
                    )
                return {
                    "task_id": validated["task_id"],
                    "bearing_id": validated["bearing_id"],
                    "edge_node_id": validated["edge_node_id"],
                    "saved": True,
                    "duplicate": True,
                }
            connection.execute(
                "INSERT INTO bearing_execution_result("
                "task_id,bearing_id,edge_node_id,execution_status,processed_packet_count,"
                "processing_latency_ms,completed_at_ns,reason_code,result_received_at_ns"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    validated["task_id"],
                    validated["bearing_id"],
                    validated["edge_node_id"],
                    validated["execution_status"],
                    validated["processed_packet_count"],
                    validated["processing_latency_ms"],
                    validated["completed_at_ns"],
                    validated["reason_code"],
                    now,
                ),
            )
        return {
            "task_id": validated["task_id"],
            "bearing_id": validated["bearing_id"],
            "edge_node_id": validated["edge_node_id"],
            "saved": True,
            "duplicate": False,
        }

    def stability_score(self, edge_node_id: str) -> float:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT execution_status FROM ("
                "  SELECT result_received_at_ns, execution_status"
                "  FROM bearing_execution_result"
                "  WHERE edge_node_id = ?"
                "  UNION ALL"
                "  SELECT result_received_at_ns, execution_status"
                "  FROM task_assignment"
                "  WHERE edge_node_id = ? AND execution_status IS NOT NULL"
                ") ORDER BY result_received_at_ns DESC LIMIT 20",
                (edge_node_id, edge_node_id),
            ).fetchall()
        if not rows:
            return 50.0
        completed = sum(row["execution_status"] == "COMPLETED" for row in rows)
        return round(completed / len(rows) * 100.0, 4)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_assignment (
                    task_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    bearing_id TEXT,
                    bearings_json TEXT NOT NULL,
                    packet_size_bytes INTEGER NOT NULL,
                    expected_packet_count INTEGER,
                    expected_duration_ms INTEGER NOT NULL,
                    created_timestamp_ns INTEGER NOT NULL,
                    assignment_status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    edge_node_id TEXT,
                    target_topic TEXT,
                    provisional_assignments_json TEXT,
                    assignments_json TEXT,
                    assigned_at_ns INTEGER,
                    failure_code TEXT,
                    scheduling_owner TEXT,
                    lease_expires_at_ns INTEGER,
                    execution_status TEXT,
                    processed_packet_count INTEGER,
                    processing_latency_ms REAL,
                    completed_at_ns INTEGER,
                    result_reason_code TEXT,
                    result_received_at_ns INTEGER,
                    updated_at_ns INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assignment_attempt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    bearing_id TEXT,
                    edge_node_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    ack_status TEXT NOT NULL,
                    reason_code TEXT,
                    started_at_ns INTEGER NOT NULL,
                    finished_at_ns INTEGER,
                    UNIQUE(task_id, attempt_number)
                );

                CREATE INDEX IF NOT EXISTS idx_task_assignment_edge_result
                ON task_assignment(edge_node_id, completed_at_ns DESC);

                CREATE INDEX IF NOT EXISTS idx_task_assignment_edge_received
                ON task_assignment(edge_node_id, result_received_at_ns DESC);

                CREATE TABLE IF NOT EXISTS bearing_execution_result (
                    task_id TEXT NOT NULL,
                    bearing_id TEXT NOT NULL,
                    edge_node_id TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    processed_packet_count INTEGER NOT NULL,
                    processing_latency_ms REAL NOT NULL,
                    completed_at_ns INTEGER NOT NULL,
                    reason_code TEXT,
                    result_received_at_ns INTEGER NOT NULL,
                    PRIMARY KEY(task_id, bearing_id)
                );

                CREATE INDEX IF NOT EXISTS idx_bearing_result_edge
                ON bearing_execution_result(edge_node_id, result_received_at_ns DESC);
                """
            )
            _ensure_columns(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # AUD-09: `with sqlite3.Connection` only commits; the connection must
        # also be closed explicitly or GC raises ResourceWarning.
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.sqlite_timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _validate_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TaskRepositoryError("INVALID_TASK_RESULT", "result must be an object")
    task_id = _non_empty_text(payload.get("task_id"), "task_id")
    raw_bearing_id = payload.get("bearing_id")
    bearing_id = (
        None
        if raw_bearing_id is None
        else _non_empty_text(raw_bearing_id, "bearing_id")
    )
    edge_node_id = _non_empty_text(payload.get("edge_node_id"), "edge_node_id")
    execution_status = _non_empty_text(
        payload.get("execution_status"), "execution_status"
    ).upper()
    if execution_status not in {"COMPLETED", "FAILED"}:
        raise TaskRepositoryError(
            "INVALID_TASK_RESULT",
            "execution_status must be COMPLETED or FAILED",
        )
    processed_packet_count = _non_negative_int(
        payload.get("processed_packet_count"), "processed_packet_count"
    )
    processing_latency_ms = _non_negative_number(
        payload.get("processing_latency_ms"), "processing_latency_ms"
    )
    completed_at_ns = _positive_int(payload.get("completed_at_ns"), "completed_at_ns")
    reason_code = payload.get("reason_code")
    if reason_code is not None and (not isinstance(reason_code, str) or not reason_code.strip()):
        raise TaskRepositoryError(
            "INVALID_TASK_RESULT", "reason_code must be null or a non-empty string"
        )
    return {
        "task_id": task_id,
        "bearing_id": bearing_id,
        "edge_node_id": edge_node_id,
        "execution_status": execution_status,
        "processed_packet_count": processed_packet_count,
        "processing_latency_ms": processing_latency_ms,
        "completed_at_ns": completed_at_ns,
        "reason_code": reason_code.strip() if isinstance(reason_code, str) else None,
    }


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskRepositoryError("INVALID_TASK_RESULT", f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TaskRepositoryError("INVALID_TASK_RESULT", f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskRepositoryError(
            "INVALID_TASK_RESULT", f"{field} must be a non-negative integer"
        )
    return value


def _non_negative_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise TaskRepositoryError(
            "INVALID_TASK_RESULT", f"{field} must be a non-negative number"
        )
    return float(value)


def _ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(task_assignment)").fetchall()
    }
    required = {
        "device_id": "TEXT",
        "bearing_id": "TEXT",
        "bearings_json": "TEXT",
        "expected_packet_count": "INTEGER",
        "scheduling_owner": "TEXT",
        "lease_expires_at_ns": "INTEGER",
        "result_received_at_ns": "INTEGER",
        "assignments_json": "TEXT",
        "provisional_assignments_json": "TEXT",
    }
    for name, column_type in required.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE task_assignment ADD COLUMN {name} {column_type}"
            )
    attempt_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(assignment_attempt)").fetchall()
    }
    if "bearing_id" not in attempt_columns:
        connection.execute("ALTER TABLE assignment_attempt ADD COLUMN bearing_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_assignment_edge_received"
        " ON task_assignment(edge_node_id, result_received_at_ns DESC)"
    )
