"""SQLite repository for current and historical bearing decision revisions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from core.diagnosis_contracts import BearingDecisionResult, BearingLifecycleStatus


class BearingResultRepository:
    def __init__(self, database_path: Path | str) -> None:
        self._database_path = str(database_path)
        self._initialize()

    def save_revision(self, draft: BearingDecisionResult) -> BearingDecisionResult:
        """Atomically supersede the current result for one bearing/round."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT result_id, revision FROM bearing_decision_result
                WHERE device_id = ? AND task_id = ? AND decision_round_id = ?
                  AND bearing_id = ? AND is_current = 1
                """,
                (draft.device_id, draft.task_id, draft.decision_round_id, draft.bearing_id),
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            result = replace(
                draft,
                result_id=f"bearing_{draft.decision_round_id}_{draft.bearing_id}_r{revision}",
                revision=revision,
                replaces_result_id=None if row is None else str(row["result_id"]),
            )
            if row is not None:
                connection.execute(
                    "UPDATE bearing_decision_result SET is_current = 0 WHERE result_id = ?",
                    (row["result_id"],),
                )
            connection.execute(
                """
                INSERT INTO bearing_decision_result (
                    result_id, device_id, task_id, bearing_id, decision_round_id,
                    revision, is_current, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    result.result_id,
                    result.device_id,
                    result.task_id,
                    result.bearing_id,
                    result.decision_round_id,
                    result.revision,
                    _serialize(result),
                ),
            )
            return result

    def get_current(
        self, device_id: str, task_id: str, decision_round_id: str, bearing_id: str
    ) -> BearingDecisionResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM bearing_decision_result
                WHERE device_id = ? AND task_id = ? AND decision_round_id = ?
                  AND bearing_id = ? AND is_current = 1
                """,
                (device_id, task_id, decision_round_id, bearing_id),
            ).fetchone()
        return None if row is None else _deserialize(str(row["payload_json"]))

    def list_current_round(
        self, device_id: str, task_id: str, decision_round_id: str
    ) -> tuple[BearingDecisionResult, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM bearing_decision_result
                WHERE device_id=? AND task_id=? AND decision_round_id=? AND is_current=1
                ORDER BY bearing_id""",
                (device_id, task_id, decision_round_id),
            ).fetchall()
        return tuple(_deserialize(str(row["payload_json"])) for row in rows)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bearing_decision_result (
                    result_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    bearing_id TEXT NOT NULL,
                    decision_round_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    is_current INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_bearing_decision_current
                ON bearing_decision_result(device_id, task_id, decision_round_id, bearing_id)
                WHERE is_current = 1
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _serialize(result: BearingDecisionResult) -> str:
    payload = asdict(result)
    payload["lifecycle_state"] = result.lifecycle_state.value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deserialize(payload_json: str) -> BearingDecisionResult:
    payload = json.loads(payload_json)
    payload["lifecycle_state"] = BearingLifecycleStatus(payload["lifecycle_state"])
    return BearingDecisionResult(**payload)
