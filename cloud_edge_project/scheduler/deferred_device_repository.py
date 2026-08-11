"""SQLite persistence for deferred device-level cloud arbitration tasks."""
# 该模块使用 SQLite 持久化延期执行的设备级云端仲裁任务。

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
_TASK_ROUTES = {"LOCAL_PROVISIONAL_AND_DEFER_CLOUD", "CLOUD_ARBITRATION_NOW"}
_RESULT_STATUSES = {"SUCCESS", "RETRYABLE_FAILED", "PERMANENT_FAILED"}


class DeferredDeviceArbitrationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class DeferredDeviceArbitrationRepository:
    def __init__(self, database_path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parents[1] / "data" / "scheduler.db"
        self.database_path = Path(
            database_path or os.getenv("SCHEDULER_DB_PATH", str(default))
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        initial_state: str = "PENDING",
    ) -> dict[str, Any]:
        item = _validate_task(payload)
        if initial_state not in {"PENDING", "WAITING_RESULT"}:
            raise DeferredDeviceArbitrationError(
                "INVALID_DEFERRED_DEVICE_TASK",
                "initial_state is invalid",
            )
        canonical = _canonical(item)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM deferred_device_arbitration_task WHERE decision_id=?",
                (item["decision_id"],),
            ).fetchone()
            if existing is not None:
                if existing["task_payload_json"] != canonical:
                    raise DeferredDeviceArbitrationError(
                        "DEFERRED_DEVICE_TASK_CONFLICT",
                        "decision_id already refers to another deferred device task",
                        409,
                    )
                return _row(existing)
            connection.execute(
                "INSERT INTO deferred_device_arbitration_task("
                "decision_id,cloud_task_id,device_id,task_id,summary_module_id,"
                "route,reason_codes_json,defer_reason,cloud_status_message_id,"
                "network_snapshot_id,bearing_results_ref,provisional_result_ref,"
                "cloud_node_id,endpoint,state,attempt_count,next_retry_at_ns,"
                "lease_expires_at_ns,created_at_ns,updated_at_ns,expires_at_ns,"
                "arbitration_id,last_reason_code,arbitration_result_json,"
                "task_payload_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,NULL,NULL,?)",
                (
                    item["decision_id"],
                    item["cloud_task_id"],
                    item["device_id"],
                    item["task_id"],
                    item["summary_module_id"],
                    item["route"],
                    _canonical(item["reason_codes"]),
                    item["defer_reason"],
                    item["cloud_status_message_id"],
                    item["network_snapshot_id"],
                    item["bearing_results_ref"],
                    item["provisional_result_ref"],
                    item["cloud_node_id"],
                    item["endpoint"],
                    initial_state,
                    1 if initial_state == "WAITING_RESULT" else 0,
                    item["created_at_ns"],
                    item["created_at_ns"],
                    item["created_at_ns"],
                    item["expires_at_ns"],
                    canonical,
                ),
            )
            saved = connection.execute(
                "SELECT * FROM deferred_device_arbitration_task WHERE decision_id=?",
                (item["decision_id"],),
            ).fetchone()
        return _row(saved)

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deferred_device_arbitration_task WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def claim_due(
        self,
        *,
        now_ns: int | None = None,
        lease_ns: int = DEFAULT_LEASE_NS,
    ) -> dict[str, Any] | None:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        lease = _positive_int(lease_ns, "lease_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_due_locked(connection, now)
            row = connection.execute(
                "SELECT decision_id FROM deferred_device_arbitration_task "
                "WHERE state='PENDING' AND next_retry_at_ns<=? AND expires_at_ns>? "
                "ORDER BY next_retry_at_ns,created_at_ns LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            decision_id = row["decision_id"]
            changed = connection.execute(
                "UPDATE deferred_device_arbitration_task SET state='DISPATCHING',"
                "attempt_count=attempt_count+1,lease_expires_at_ns=?,updated_at_ns=? "
                "WHERE decision_id=? AND state='PENDING'",
                (now + lease, now, decision_id),
            ).rowcount
            if changed != 1:
                return None
            claimed = self._required(connection, decision_id)
        return _row(claimed)

    def mark_dispatched(
        self,
        decision_id: str,
        *,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, decision_id)
            if row["state"] == "WAITING_RESULT" or row["state"] in _TERMINAL_STATES:
                return _row(row)
            if row["state"] != "DISPATCHING":
                raise DeferredDeviceArbitrationError(
                    "INVALID_DEFERRED_DEVICE_STATE",
                    "task is not dispatching",
                    409,
                )
            connection.execute(
                "UPDATE deferred_device_arbitration_task "
                "SET state='WAITING_RESULT',updated_at_ns=? WHERE decision_id=?",
                (now, decision_id),
            )
            saved = self._required(connection, decision_id)
        return _row(saved)


    def schedule_retry(
        self,
        decision_id: str,
        *,
        reason_code: str,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns() if now_ns is None else _positive_int(now_ns, "now_ns")
        reason = _text(reason_code, "reason_code")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, decision_id)
            if row["state"] not in {"DISPATCHING", "WAITING_RESULT"}:
                raise DeferredDeviceArbitrationError(
                    "INVALID_DEFERRED_DEVICE_STATE",
                    "task cannot be retried",
                    409,
                )
            next_retry = now + _retry_delay_ns(int(row["attempt_count"]))
            connection.execute(
                "UPDATE deferred_device_arbitration_task SET state='PENDING',"
                "next_retry_at_ns=?,lease_expires_at_ns=NULL,updated_at_ns=?,"
                "last_reason_code=? WHERE decision_id=?",
                (next_retry, now, reason, decision_id),
            )
            saved = self._required(connection, decision_id)
        return _row(saved)

    def save_arbitration_result(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _validate_arbitration_result(payload)
        canonical = _canonical(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required(connection, result["decision_id"])
            _match_result_identity(row, result)
            if row["arbitration_result_json"] == canonical:
                return _row(row)
            if row["state"] in _TERMINAL_STATES:
                raise DeferredDeviceArbitrationError(
                    "DEVICE_ARBITRATION_RESULT_CONFLICT",
                    "terminal decision already has another arbitration result",
                    409,
                )
            if result["arbitration_status"] == "SUCCESS":
                state = "SUCCEEDED"
                next_retry = row["next_retry_at_ns"]
                arbitration_id = result["arbitration_id"]
            elif result["arbitration_status"] == "PERMANENT_FAILED":
                state = "PERMANENT_FAILED"
                next_retry = row["next_retry_at_ns"]
                arbitration_id = None
            else:
                state = "PENDING"
                next_retry = result["reported_at_ns"] + _retry_delay_ns(
                    int(row["attempt_count"])
                )
                arbitration_id = None
            connection.execute(
                "UPDATE deferred_device_arbitration_task SET state=?,"
                "next_retry_at_ns=?,lease_expires_at_ns=NULL,updated_at_ns=?,"
                "arbitration_id=?,last_reason_code=?,arbitration_result_json=? "
                "WHERE decision_id=?",
                (
                    state,
                    next_retry,
                    result["reported_at_ns"],
                    arbitration_id,
                    result["reason_code"],
                    canonical,
                    result["decision_id"],
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
                "UPDATE deferred_device_arbitration_task SET state='PENDING',"
                "next_retry_at_ns=?,lease_expires_at_ns=NULL,updated_at_ns=?,"
                "last_reason_code='SCHEDULER_RESTART' "
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
                "UPDATE deferred_device_arbitration_task SET state='EXPIRED',"
                "updated_at_ns=?,lease_expires_at_ns=NULL,"
                "last_reason_code='RETENTION_EXPIRED' "
                "WHERE state NOT IN ('SUCCEEDED','PERMANENT_FAILED','EXPIRED') "
                "AND expires_at_ns<=?",
                (now_ns, now_ns),
            ).rowcount
        )

    def _required(self, connection: sqlite3.Connection, decision_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM deferred_device_arbitration_task WHERE decision_id=?",
            (_text(decision_id, "decision_id"),),
        ).fetchone()
        if row is None:
            raise DeferredDeviceArbitrationError(
                "DEFERRED_DEVICE_TASK_NOT_FOUND",
                "deferred device task not found",
                404,
            )
        return row

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deferred_device_arbitration_task (
                    decision_id TEXT PRIMARY KEY,
                    cloud_task_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    summary_module_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    defer_reason TEXT,
                    cloud_status_message_id TEXT,
                    network_snapshot_id TEXT,
                    bearing_results_ref TEXT NOT NULL,
                    provisional_result_ref TEXT,
                    cloud_node_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    next_retry_at_ns INTEGER NOT NULL,
                    lease_expires_at_ns INTEGER,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    expires_at_ns INTEGER NOT NULL,
                    arbitration_id TEXT,
                    last_reason_code TEXT,
                    arbitration_result_json TEXT,
                    task_payload_json TEXT NOT NULL
                )
                """
            )


def _validate_task(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = _mapping(payload, "deferred device task")
        route = _enum(item.get("route"), "route", _TASK_ROUTES)
        created_at_ns = _positive_int(item.get("created_at_ns"), "created_at_ns")
        expires_at_ns = _positive_int(item.get("expires_at_ns"), "expires_at_ns")
        if expires_at_ns <= created_at_ns:
            raise ValueError("expires_at_ns must be after created_at_ns")
        return {
            "decision_id": _text(item.get("decision_id"), "decision_id"),
            "cloud_task_id": _text(item.get("cloud_task_id"), "cloud_task_id"),
            "device_id": _text(item.get("device_id"), "device_id"),
            "task_id": _text(item.get("task_id"), "task_id"),
            "summary_module_id": _text(item.get("summary_module_id"), "summary_module_id"),
            "route": route,
            "reason_codes": _string_list(item.get("reason_codes"), "reason_codes"),
            "defer_reason": _optional_text(item.get("defer_reason"), "defer_reason"),
            "cloud_status_message_id": _optional_text(item.get("cloud_status_message_id"), "cloud_status_message_id"),
            "network_snapshot_id": _optional_text(item.get("network_snapshot_id"), "network_snapshot_id"),
            "bearing_results_ref": _text(item.get("bearing_results_ref"), "bearing_results_ref"),
            "provisional_result_ref": _optional_text(item.get("provisional_result_ref"), "provisional_result_ref"),
            "cloud_node_id": _text(item.get("cloud_node_id"), "cloud_node_id"),
            "endpoint": _text(item.get("endpoint"), "endpoint"),
            "created_at_ns": created_at_ns,
            "expires_at_ns": expires_at_ns,
        }
    except (KeyError, TypeError, ValueError) as error:
        raise DeferredDeviceArbitrationError("INVALID_DEFERRED_DEVICE_TASK", str(error)) from error


def _validate_arbitration_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        item = _mapping(payload, "device arbitration result")
        status = _enum(item.get("arbitration_status"), "arbitration_status", _RESULT_STATUSES)
        arbitration_id = _optional_text(item.get("arbitration_id"), "arbitration_id")
        reason_code = _optional_text(item.get("reason_code"), "reason_code")
        if status == "SUCCESS" and arbitration_id is None:
            raise ValueError("successful arbitration requires arbitration_id")
        if status != "SUCCESS" and reason_code is None:
            raise ValueError("failed arbitration requires reason_code")
        return {
            "decision_id": _text(item.get("decision_id"), "decision_id"),
            "cloud_task_id": _text(item.get("cloud_task_id"), "cloud_task_id"),
            "device_id": _text(item.get("device_id"), "device_id"),
            "task_id": _text(item.get("task_id"), "task_id"),
            "summary_module_id": _text(item.get("summary_module_id"), "summary_module_id"),
            "arbitration_status": status,
            "arbitration_id": arbitration_id,
            "reason_code": reason_code,
            "reported_at_ns": _positive_int(item.get("reported_at_ns"), "reported_at_ns"),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise DeferredDeviceArbitrationError("INVALID_DEVICE_ARBITRATION_RESULT", str(error)) from error


def _match_result_identity(row: sqlite3.Row, result: Mapping[str, Any]) -> None:
    for field in ("decision_id", "cloud_task_id", "device_id", "task_id", "summary_module_id"):
        if row[field] != result[field]:
            raise DeferredDeviceArbitrationError(
                "DEVICE_ARBITRATION_RESULT_CONFLICT",
                f"arbitration result conflicts on {field}",
                409,
            )


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "cloud_task_id": row["cloud_task_id"],
        "device_id": row["device_id"],
        "task_id": row["task_id"],
        "summary_module_id": row["summary_module_id"],
        "route": row["route"],
        "reason_codes": json.loads(row["reason_codes_json"]),
        "defer_reason": row["defer_reason"],
        "cloud_status_message_id": row["cloud_status_message_id"],
        "network_snapshot_id": row["network_snapshot_id"],
        "bearing_results_ref": row["bearing_results_ref"],
        "provisional_result_ref": row["provisional_result_ref"],
        "cloud_node_id": row["cloud_node_id"],
        "endpoint": row["endpoint"],
        "state": row["state"],
        "attempt_count": int(row["attempt_count"]),
        "next_retry_at_ns": int(row["next_retry_at_ns"]),
        "lease_expires_at_ns": row["lease_expires_at_ns"],
        "created_at_ns": int(row["created_at_ns"]),
        "updated_at_ns": int(row["updated_at_ns"]),
        "expires_at_ns": int(row["expires_at_ns"]),
        "arbitration_id": row["arbitration_id"],
        "last_reason_code": row["last_reason_code"],
        "arbitration_result": (
            json.loads(row["arbitration_result_json"])
            if row["arbitration_result_json"] is not None
            else None
        ),
    }


def _retry_delay_ns(attempt_count: int) -> int:
    index = min(max(attempt_count - 1, 0), len(_RETRY_SECONDS) - 1)
    return _RETRY_SECONDS[index] * 1_000_000_000


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _text(value, field).upper()
    if normalized not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return [_text(item, field) for item in value]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
