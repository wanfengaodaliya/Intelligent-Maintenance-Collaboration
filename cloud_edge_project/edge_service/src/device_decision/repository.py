"""SQLite source of truth and CAS close authority for device rounds."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from core.diagnosis_contracts import (
    DeviceDecisionResult,
    DeviceDecisionStatus,
    RoundClosureReason,
)


class DeviceDecisionRoundRepository:
    def __init__(self, database_path: Path | str) -> None:
        self._database_path = str(database_path)
        self._initialize()

    def register_round(
        self,
        *,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        expected_bearing_ids: tuple[str, ...],
        opened_at_ns: int,
    ) -> dict:
        if not expected_bearing_ids or len(set(expected_bearing_ids)) != len(expected_bearing_ids):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        expected_json = json.dumps(expected_bearing_ids, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM device_decision_round WHERE device_id=? AND task_id=? AND decision_round_id=?",
                (device_id, task_id, decision_round_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO device_decision_round(
                    device_id,task_id,decision_round_id,expected_bearing_ids_json,state,
                    closure_reason,opened_at_ns,closed_at_ns,version
                    ) VALUES (?,?,?,?, 'OPEN', NULL, ?, NULL, 1)""",
                    (device_id, task_id, decision_round_id, expected_json, opened_at_ns),
                )
                row = connection.execute(
                    "SELECT * FROM device_decision_round WHERE device_id=? AND task_id=? AND decision_round_id=?",
                    (device_id, task_id, decision_round_id),
                ).fetchone()
            elif row["expected_bearing_ids_json"] != expected_json:
                raise ValueError("expected_bearing_ids conflict for decision round")
        return _row(row)

    def get_round(self, device_id: str, task_id: str, decision_round_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_decision_round WHERE device_id=? AND task_id=? AND decision_round_id=?",
                (device_id, task_id, decision_round_id),
            ).fetchone()
        return None if row is None else _row(row)

    def list_open_due(self, *, now_ns: int, round_timeout_ns: int) -> tuple[dict, ...]:
        if round_timeout_ns <= 0:
            raise ValueError("round_timeout_ns must be positive")
        deadline = now_ns - round_timeout_ns
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM device_decision_round
                WHERE state='OPEN' AND opened_at_ns<=?
                ORDER BY opened_at_ns, device_id, task_id, decision_round_id""",
                (deadline,),
            ).fetchall()
        return tuple(_row(row) for row in rows)

    def close_round(
        self,
        *,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        expected_version: int,
        closure_reason: RoundClosureReason,
        closed_at_ns: int,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE device_decision_round
                SET state='CLOSED', closure_reason=?, closed_at_ns=?, version=version+1
                WHERE device_id=? AND task_id=? AND decision_round_id=?
                  AND state='OPEN' AND version=?""",
                (closure_reason.value, closed_at_ns, device_id, task_id, decision_round_id, expected_version),
            ).rowcount
        return changed == 1

    def save_revision(self, draft: DeviceDecisionResult) -> DeviceDecisionResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT result_id, revision FROM device_decision_result
                WHERE device_id=? AND task_id=? AND decision_round_id=? AND is_current=1""",
                (draft.device_id, draft.task_id, draft.decision_round_id),
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            result = replace(
                draft,
                result_id=f"device_{draft.decision_round_id}_r{revision}",
                revision=revision,
                replaces_result_id=None if row is None else str(row["result_id"]),
            )
            if row is not None:
                connection.execute(
                    "UPDATE device_decision_result SET is_current=0 WHERE result_id=?",
                    (row["result_id"],),
                )
            connection.execute(
                """INSERT INTO device_decision_result(
                result_id,device_id,task_id,decision_round_id,revision,is_current,payload_json
                ) VALUES (?,?,?,?,?,?,?)""",
                (result.result_id, result.device_id, result.task_id, result.decision_round_id,
                 result.revision, 1, _serialize_result(result)),
            )
        return result

    def get_current_result(
        self, device_id: str, task_id: str, decision_round_id: str
    ) -> DeviceDecisionResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM device_decision_result
                WHERE device_id=? AND task_id=? AND decision_round_id=? AND is_current=1""",
                (device_id, task_id, decision_round_id),
            ).fetchone()
        return None if row is None else _deserialize_result(str(row["payload_json"]))

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS device_decision_round(
                device_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_round_id TEXT NOT NULL,
                expected_bearing_ids_json TEXT NOT NULL, state TEXT NOT NULL,
                closure_reason TEXT, opened_at_ns INTEGER NOT NULL, closed_at_ns INTEGER,
                version INTEGER NOT NULL,
                PRIMARY KEY(device_id, task_id, decision_round_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS device_decision_result(
                result_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, task_id TEXT NOT NULL,
                decision_round_id TEXT NOT NULL, revision INTEGER NOT NULL,
                is_current INTEGER NOT NULL, payload_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_device_decision_current
                ON device_decision_result(device_id,task_id,decision_round_id)
                WHERE is_current=1"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _row(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["expected_bearing_ids"] = tuple(json.loads(value.pop("expected_bearing_ids_json")))
    return value


def _serialize_result(result: DeviceDecisionResult) -> str:
    value = asdict(result)
    value["status"] = result.status.value
    value["closure_reason"] = result.closure_reason.value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deserialize_result(payload_json: str) -> DeviceDecisionResult:
    value = json.loads(payload_json)
    value["status"] = DeviceDecisionStatus(value["status"])
    value["closure_reason"] = RoundClosureReason(value["closure_reason"])
    for field in (
        "expected_bearing_ids", "received_bearing_ids", "missing_bearing_ids",
        "bearing_result_ids", "conflict_reasons",
    ):
        value[field] = tuple(value[field])
    return DeviceDecisionResult(**value)
