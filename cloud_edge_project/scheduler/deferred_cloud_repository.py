"""SQLite persistence for deferred package-level cloud review tasks."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


DAY_NS = 86_400_000_000_000
DEFAULT_LEASE_NS = 30_000_000_000
_RETRY_SECONDS = (5, 10, 20, 40, 60)
_TERMINAL_STATES = {"SUCCEEDED", "PERMANENT_FAILED", "EXPIRED"}


class DeferredCloudError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class DeferredCloudRepository:
    def __init__(self, database_path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parents[1] / "data" / "scheduler.db"
        self.database_path = Path(
            database_path or os.getenv("SCHEDULER_DB_PATH", str(default))
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self, payload: Mapping[str, Any], *, initial_state: str = "PENDING"
    ) -> dict[str, Any]:
        item = _validate_task(payload)
        if initial_state not in {"PENDING", "WAITING_RESULT"}:
            raise DeferredCloudError("INVALID_DEFERRED_TASK", "initial_state is invalid")
        canonical = _canonical(item)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM deferred_cloud_task WHERE decision_id=?",
                (item["decision_id"],),
            ).fetchone()
            if existing is not None:
                if existing["task_payload_json"] != canonical:
                    raise DeferredCloudError(
                        "DEFERRED_TASK_CONFLICT",
                        "decision_id already refers to another deferred task",
                        409,
                    )
                return _row(existing)
            connection.execute(
                "INSERT INTO deferred_cloud_task("
                "decision_id,cloud_task_id,device_id,task_id,bearing_id,packet_id,"
                "sequence_number,edge_node_id,route,reason_codes_json,defer_reason,"
                "cloud_status_message_id,network_snapshot_id,raw_data_ref,context_ref,"
                "cloud_node_id,endpoint,state,attempt_count,next_retry_at_ns,"
                "lease_expires_at_ns,created_at_ns,updated_at_ns,expires_at_ns,"
                "review_id,last_reason_code,upload_result_json,task_payload_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,NULL,NULL,?)",
                (
                    item["decision_id"], item["cloud_task_id"], item["device_id"],
                    item["task_id"], item["bearing_id"], item["packet_id"],
                    item["sequence_number"], item["edge_node_id"], item["route"],
                    _canonical(item["reason_codes"]), item["defer_reason"],
                    item["cloud_status_message_id"], item["network_snapshot_id"],
                    item["raw_data_ref"], item["context_ref"], item["cloud_node_id"],
                    item["endpoint"], initial_state,
                    1 if initial_state == "WAITING_RESULT" else 0,
                    item["created_at_ns"], item["created_at_ns"], item["created_at_ns"],
                    item["expires_at_ns"], canonical,
                ),
            )
            saved = connection.execute(
                "SELECT * FROM deferred_cloud_task WHERE decision_id=?",
                (item["decision_id"],),
            ).fetchone()
        return _row(saved)

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deferred_cloud_task WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def claim_due(
        self, *, now_ns: int | None = None, lease_ns: int = DEFAULT_LEASE_NS
    ) -> dict[str, Any] | None:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        lease = _positive_int(lease_ns, "lease_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due_locked(connection, now)
            row = connection.execute(
                "SELECT decision_id FROM deferred_cloud_task "
                "WHERE state='PENDING' AND next_retry_at_ns<=? AND expires_at_ns>? "
                "ORDER BY next_retry_at_ns,created_at_ns LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            decision_id = row["decision_id"]
            changed = connection.execute(
                "UPDATE deferred_cloud_task SET state='DISPATCHING',"
                "attempt_count=attempt_count+1,lease_expires_at_ns=?,updated_at_ns=? "
                "WHERE decision_id=? AND state='PENDING'",
                (now + lease, now, decision_id),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM deferred_cloud_task WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return _row(claimed)

    def mark_dispatched(self, decision_id: str, *, now_ns: int | None = None) -> dict[str, Any]:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, decision_id)
            if row["state"] == "WAITING_RESULT" or row["state"] in _TERMINAL_STATES:
                return _row(row)
            if row["state"] != "DISPATCHING":
                raise DeferredCloudError("INVALID_DEFERRED_STATE", "task is not dispatching", 409)
            connection.execute(
                "UPDATE deferred_cloud_task SET state='WAITING_RESULT',updated_at_ns=? "
                "WHERE decision_id=?",
                (now, decision_id),
            )
            saved = self._required(connection, decision_id)
        return _row(saved)

    def schedule_retry(
        self, decision_id: str, *, reason_code: str, now_ns: int | None = None
    ) -> dict[str, Any]:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        reason = _text(reason_code, "reason_code")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, decision_id)
            if row["state"] not in {"DISPATCHING", "WAITING_RESULT"}:
                raise DeferredCloudError("INVALID_DEFERRED_STATE", "task cannot be retried", 409)
            next_retry = now + _retry_delay_ns(int(row["attempt_count"]))
            connection.execute(
                "UPDATE deferred_cloud_task SET state='PENDING',next_retry_at_ns=?,"
                "lease_expires_at_ns=NULL,updated_at_ns=?,last_reason_code=? "
                "WHERE decision_id=?",
                (next_retry, now, reason, decision_id),
            )
            saved = self._required(connection, decision_id)
        return _row(saved)

    def save_upload_result(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _validate_upload_result(payload)
        canonical = _canonical(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, result["decision_id"])
            _match_result_identity(row, result)
            if row["upload_result_json"] == canonical:
                return _row(row)
            if row["state"] in _TERMINAL_STATES:
                raise DeferredCloudError(
                    "UPLOAD_RESULT_CONFLICT",
                    "terminal decision already has another upload result",
                    409,
                )
            if result["upload_status"] == "SUCCESS":
                state = "SUCCEEDED"
                next_retry = row["next_retry_at_ns"]
                review_id = result["review_id"]
            elif result["upload_status"] == "PERMANENT_FAILED":
                state = "PERMANENT_FAILED"
                next_retry = row["next_retry_at_ns"]
                review_id = None
            else:
                state = "PENDING"
                next_retry = result["reported_at_ns"] + _retry_delay_ns(int(row["attempt_count"]))
                review_id = None
            connection.execute(
                "UPDATE deferred_cloud_task SET state=?,next_retry_at_ns=?,"
                "lease_expires_at_ns=NULL,updated_at_ns=?,review_id=?,last_reason_code=?,"
                "upload_result_json=? WHERE decision_id=?",
                (
                    state, next_retry, result["reported_at_ns"], review_id,
                    result["reason_code"], canonical, result["decision_id"],
                ),
            )
            saved = self._required(connection, result["decision_id"])
        return _row(saved)

    def recover_non_terminal(self, *, now_ns: int | None = None) -> int:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due_locked(connection, now)
            changed = connection.execute(
                "UPDATE deferred_cloud_task SET state='PENDING',next_retry_at_ns=?,"
                "lease_expires_at_ns=NULL,updated_at_ns=?,last_reason_code='SCHEDULER_RESTART' "
                "WHERE state IN ('DISPATCHING','WAITING_RESULT') AND expires_at_ns>?",
                (now, now, now),
            ).rowcount
        return int(changed)

    def expire_due(self, *, now_ns: int | None = None) -> int:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._expire_due_locked(connection, now)
        return changed

    @staticmethod
    def _expire_due_locked(connection: sqlite3.Connection, now_ns: int) -> int:
        return int(
            connection.execute(
                "UPDATE deferred_cloud_task SET state='EXPIRED',updated_at_ns=?,"
                "lease_expires_at_ns=NULL,last_reason_code='RETENTION_EXPIRED' "
                "WHERE state NOT IN ('SUCCEEDED','PERMANENT_FAILED','EXPIRED') "
                "AND expires_at_ns<=?",
                (now_ns, now_ns),
            ).rowcount
        )

    @staticmethod
    def _required(connection: sqlite3.Connection, decision_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM deferred_cloud_task WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise DeferredCloudError("DEFERRED_TASK_NOT_FOUND", "decision_id was not found", 404)
        return row

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deferred_cloud_task("
                "decision_id TEXT PRIMARY KEY,cloud_task_id TEXT NOT NULL UNIQUE,"
                "device_id TEXT NOT NULL,task_id TEXT NOT NULL,bearing_id TEXT NOT NULL,"
                "packet_id TEXT NOT NULL,sequence_number INTEGER NOT NULL,"
                "edge_node_id TEXT NOT NULL,route TEXT NOT NULL,reason_codes_json TEXT NOT NULL,"
                "defer_reason TEXT,cloud_status_message_id TEXT,network_snapshot_id TEXT,"
                "raw_data_ref TEXT NOT NULL,context_ref TEXT,cloud_node_id TEXT NOT NULL,"
                "endpoint TEXT NOT NULL,state TEXT NOT NULL,attempt_count INTEGER NOT NULL,"
                "next_retry_at_ns INTEGER NOT NULL,lease_expires_at_ns INTEGER,"
                "created_at_ns INTEGER NOT NULL,updated_at_ns INTEGER NOT NULL,"
                "expires_at_ns INTEGER NOT NULL,review_id TEXT,last_reason_code TEXT,"
                "upload_result_json TEXT,task_payload_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deferred_due "
                "ON deferred_cloud_task(state,next_retry_at_ns)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=0.2)
        connection.row_factory = sqlite3.Row
        return connection


def _validate_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
        "packet_id", "sequence_number", "edge_node_id", "route", "reason_codes",
        "defer_reason", "cloud_status_message_id", "network_snapshot_id", "raw_data_ref",
        "context_ref", "cloud_node_id", "endpoint", "created_at_ns", "expires_at_ns",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise DeferredCloudError("INVALID_DEFERRED_TASK", "deferred task fields do not match contract")
    try:
        reasons = payload["reason_codes"]
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("reason_codes must be a non-empty array")
        item = {
            field: _text(payload[field], field)
            for field in (
                "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
                "packet_id", "edge_node_id", "route", "raw_data_ref", "cloud_node_id", "endpoint",
            )
        }
        item.update(
            {
                "sequence_number": _bounded_int(payload["sequence_number"], 1, 80),
                "reason_codes": [_text(reason, "reason_code") for reason in reasons],
                "defer_reason": _optional_text(payload["defer_reason"], "defer_reason"),
                "cloud_status_message_id": _optional_text(payload["cloud_status_message_id"], "cloud_status_message_id"),
                "network_snapshot_id": _optional_text(payload["network_snapshot_id"], "network_snapshot_id"),
                "context_ref": _optional_text(payload["context_ref"], "context_ref"),
                "created_at_ns": _positive_int(payload["created_at_ns"], "created_at_ns"),
                "expires_at_ns": _positive_int(payload["expires_at_ns"], "expires_at_ns"),
            }
        )
        if item["expires_at_ns"] <= item["created_at_ns"]:
            raise ValueError("expires_at_ns must follow created_at_ns")
        return item
    except (TypeError, ValueError) as error:
        raise DeferredCloudError("INVALID_DEFERRED_TASK", str(error)) from error


def _validate_upload_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
        "packet_id", "edge_node_id", "upload_status", "review_id", "reason_code",
        "reported_at_ns",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise DeferredCloudError("INVALID_UPLOAD_RESULT", "upload result fields do not match contract")
    try:
        status = _text(payload["upload_status"], "upload_status").upper()
        if status not in {"SUCCESS", "RETRYABLE_FAILED", "PERMANENT_FAILED"}:
            raise ValueError("upload_status is invalid")
        result = {
            field: _text(payload[field], field)
            for field in (
                "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
                "packet_id", "edge_node_id",
            )
        }
        result.update(
            {
                "upload_status": status,
                "review_id": _optional_text(payload["review_id"], "review_id"),
                "reason_code": _optional_text(payload["reason_code"], "reason_code"),
                "reported_at_ns": _positive_int(payload["reported_at_ns"], "reported_at_ns"),
            }
        )
        if status == "SUCCESS" and (result["review_id"] is None or result["reason_code"] is not None):
            raise ValueError("SUCCESS requires review_id and reason_code=null")
        if status != "SUCCESS" and (result["review_id"] is not None or result["reason_code"] is None):
            raise ValueError("failed upload requires reason_code and review_id=null")
        return result
    except (TypeError, ValueError) as error:
        raise DeferredCloudError("INVALID_UPLOAD_RESULT", str(error)) from error


def _match_result_identity(row: sqlite3.Row, result: Mapping[str, Any]) -> None:
    for field in (
        "decision_id", "cloud_task_id", "device_id", "task_id", "bearing_id",
        "packet_id", "edge_node_id",
    ):
        if row[field] != result[field]:
            raise DeferredCloudError("UPLOAD_RESULT_CONFLICT", f"upload result conflicts on {field}", 409)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["reason_codes"] = json.loads(result.pop("reason_codes_json"))
    result.pop("task_payload_json", None)
    result.pop("upload_result_json", None)
    return result


def _retry_delay_ns(attempt_count: int) -> int:
    index = min(max(attempt_count, 1) - 1, len(_RETRY_SECONDS) - 1)
    return _RETRY_SECONDS[index] * 1_000_000_000


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"sequence_number must be between {minimum} and {maximum}")
    return value
