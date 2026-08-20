"""SQLite source of truth and CAS close authority for device rounds."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from core.diagnosis_contracts import (
    DeviceDecisionResult,
    DeviceDecisionStatus,
    RoundClosureReason,
)


DEFAULT_ROUND_TIMEOUT_NS = 3_500_000_000


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
        deadline_at_ns: int | None = None,
    ) -> dict:
        if not expected_bearing_ids or len(set(expected_bearing_ids)) != len(expected_bearing_ids):
            raise ValueError("expected_bearing_ids must be non-empty and unique")
        expected_json = json.dumps(expected_bearing_ids, separators=(",", ":"))
        deadline = (
            opened_at_ns + DEFAULT_ROUND_TIMEOUT_NS
            if deadline_at_ns is None
            else deadline_at_ns
        )
        if deadline <= opened_at_ns:
            raise ValueError("deadline_at_ns must be after opened_at_ns")
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
                    closure_reason,opened_at_ns,deadline_at_ns,closed_at_ns,version
                    ) VALUES (?,?,?,?, 'OPEN', NULL, ?, ?, NULL, 1)""",
                    (
                        device_id,
                        task_id,
                        decision_round_id,
                        expected_json,
                        opened_at_ns,
                        deadline,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM device_decision_round WHERE device_id=? AND task_id=? AND decision_round_id=?",
                    (device_id, task_id, decision_round_id),
                ).fetchone()
            elif row["expected_bearing_ids_json"] != expected_json:
                raise ValueError("expected_bearing_ids conflict for decision round")
        return _row(row)

    def get_round(
        self,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict | None:
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            row = selected.execute(
                "SELECT * FROM device_decision_round WHERE device_id=? AND task_id=? AND decision_round_id=?",
                (device_id, task_id, decision_round_id),
            ).fetchone()
        return None if row is None else _row(row)

    def list_open_due(self, *, now_ns: int) -> tuple[dict, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM device_decision_round
                WHERE state='OPEN' AND deadline_at_ns<=?
                ORDER BY deadline_at_ns, device_id, task_id, decision_round_id""",
                (now_ns,),
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

    def close_round_and_save_initial_result(
        self,
        draft: DeviceDecisionResult,
        *,
        expected_version: int,
        connection: sqlite3.Connection | None = None,
    ) -> DeviceDecisionResult | None:
        """Atomically win round closure and persist its first device result."""
        if draft.revision != 1 or draft.replaces_result_id is not None:
            raise ValueError("initial device result must be an unsuperseded revision 1 draft")
        result = replace(
            draft,
            result_id=f"device_{draft.decision_round_id}_r1",
            revision=1,
            replaces_result_id=None,
        )
        own_connection = connection is None
        with (self._connect() if own_connection else nullcontext(connection)) as selected:
            if own_connection:
                selected.execute("BEGIN IMMEDIATE")
            changed = selected.execute(
                """UPDATE device_decision_round
                SET state='CLOSED', closure_reason=?, closed_at_ns=?,
                    current_device_result_id=?, version=version+1
                WHERE device_id=? AND task_id=? AND decision_round_id=?
                  AND state='OPEN' AND version=?""",
                (
                    result.closure_reason.value,
                    result.closed_at_ns,
                    result.result_id,
                    result.device_id,
                    result.task_id,
                    result.decision_round_id,
                    expected_version,
                ),
            ).rowcount
            if changed != 1:
                return None
            selected.execute(
                """INSERT INTO device_decision_result(
                result_id,device_id,task_id,decision_round_id,revision,is_current,payload_json
                ) VALUES (?,?,?,?,?,1,?)""",
                (
                    result.result_id,
                    result.device_id,
                    result.task_id,
                    result.decision_round_id,
                    result.revision,
                    _serialize_result(result),
                ),
            )
        return result

    @contextmanager
    def transaction(self):
        """Open one immediate transaction shared by round and bearing repositories."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def save_revision(
        self,
        draft: DeviceDecisionResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> DeviceDecisionResult:
        own_connection = connection is None
        with (self._connect() if own_connection else nullcontext(connection)) as selected:
            if own_connection:
                selected.execute("BEGIN IMMEDIATE")
            row = selected.execute(
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
                selected.execute(
                    "UPDATE device_decision_result SET is_current=0 WHERE result_id=?",
                    (row["result_id"],),
                )
            selected.execute(
                """INSERT INTO device_decision_result(
                result_id,device_id,task_id,decision_round_id,revision,is_current,payload_json
                ) VALUES (?,?,?,?,?,?,?)""",
                (result.result_id, result.device_id, result.task_id, result.decision_round_id,
                 result.revision, 1, _serialize_result(result)),
            )
            selected.execute(
                """UPDATE device_decision_round SET current_device_result_id=?
                WHERE device_id=? AND task_id=? AND decision_round_id=?""",
                (
                    result.result_id,
                    result.device_id,
                    result.task_id,
                    result.decision_round_id,
                ),
            )
        return result

    def get_current_result(
        self,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> DeviceDecisionResult | None:
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            row = selected.execute(
                """SELECT payload_json FROM device_decision_result
                WHERE device_id=? AND task_id=? AND decision_round_id=? AND is_current=1""",
                (device_id, task_id, decision_round_id),
            ).fetchone()
        return None if row is None else _deserialize_result(str(row["payload_json"]))

    def get_arbitration_receipt(
        self,
        arbitration_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict | None:
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            row = selected.execute(
                "SELECT * FROM device_arbitration_receipt WHERE arbitration_id=?",
                (arbitration_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def save_arbitration_receipt(
        self,
        *,
        arbitration_id: str,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        result_id: str,
        processed_at_ns: int,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """幂等记录已处理的云仲裁回调，重复回调不再产生新修订。"""
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            changed = selected.execute(
                """INSERT OR IGNORE INTO device_arbitration_receipt(
                arbitration_id, device_id, task_id, decision_round_id, result_id, processed_at_ns
                ) VALUES (?,?,?,?,?,?)""",
                (arbitration_id, device_id, task_id, decision_round_id, result_id, processed_at_ns),
            ).rowcount
        return changed == 1

    def list_device_results(
        self,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[DeviceDecisionResult, ...]:
        """Return every persisted device revision for delivery reconciliation."""
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            rows = selected.execute(
                """SELECT payload_json FROM device_decision_result
                ORDER BY device_id, task_id, decision_round_id, revision"""
            ).fetchall()
        return tuple(_deserialize_result(str(row["payload_json"])) for row in rows)

    def count_open_rounds(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM device_decision_round WHERE state='OPEN'"
            ).fetchone()
        return int(row["total"])

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS device_decision_round(
                device_id TEXT NOT NULL, task_id TEXT NOT NULL, decision_round_id TEXT NOT NULL,
                expected_bearing_ids_json TEXT NOT NULL, state TEXT NOT NULL,
                closure_reason TEXT, opened_at_ns INTEGER NOT NULL,
                deadline_at_ns INTEGER NOT NULL, closed_at_ns INTEGER,
                version INTEGER NOT NULL, current_device_result_id TEXT,
                PRIMARY KEY(device_id, task_id, decision_round_id)
                )"""
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(device_decision_round)"
                ).fetchall()
            }
            if "current_device_result_id" not in columns:
                connection.execute(
                    "ALTER TABLE device_decision_round "
                    "ADD COLUMN current_device_result_id TEXT"
                )
            if "deadline_at_ns" not in columns:
                connection.execute(
                    "ALTER TABLE device_decision_round ADD COLUMN deadline_at_ns INTEGER"
                )
                connection.execute(
                    "UPDATE device_decision_round SET deadline_at_ns="
                    "opened_at_ns+? WHERE deadline_at_ns IS NULL",
                    (DEFAULT_ROUND_TIMEOUT_NS,),
                )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_device_round_deadline
                ON device_decision_round(state,deadline_at_ns)"""
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS device_arbitration_receipt(
                arbitration_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                task_id TEXT NOT NULL, decision_round_id TEXT NOT NULL,
                result_id TEXT NOT NULL, processed_at_ns INTEGER NOT NULL)"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # AUD-09: commit on success, rollback on error, and always close.
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


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
