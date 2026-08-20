"""SQLite repository for current and historical bearing decision revisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from core.diagnosis_contracts import BearingDecisionResult, BearingLifecycleStatus


class BearingResultRepository:
    def __init__(self, database_path: Path | str) -> None:
        self._database_path = str(database_path)
        self._initialize()

    def save_revision(
        self,
        draft: BearingDecisionResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> BearingDecisionResult:
        """Atomically supersede the current result for one bearing/round."""
        own_connection = connection is None
        with (self._connect() if own_connection else nullcontext(connection)) as selected:
            if own_connection:
                selected.execute("BEGIN IMMEDIATE")
            row = selected.execute(
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
                selected.execute(
                    "UPDATE bearing_decision_result SET is_current = 0 WHERE result_id = ?",
                    (row["result_id"],),
                )
            selected.execute(
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
        self,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        bearing_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> BearingDecisionResult | None:
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            row = selected.execute(
                """
                SELECT payload_json FROM bearing_decision_result
                WHERE device_id = ? AND task_id = ? AND decision_round_id = ?
                  AND bearing_id = ? AND is_current = 1
                """,
                (device_id, task_id, decision_round_id, bearing_id),
            ).fetchone()
        return None if row is None else _deserialize(str(row["payload_json"]))

    def list_current_round(
        self,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[BearingDecisionResult, ...]:
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            rows = selected.execute(
                """SELECT payload_json FROM bearing_decision_result
                WHERE device_id=? AND task_id=? AND decision_round_id=? AND is_current=1
                ORDER BY bearing_id""",
                (device_id, task_id, decision_round_id),
            ).fetchall()
        return tuple(_deserialize(str(row["payload_json"])) for row in rows)

    def list_waiting_cloud_due(
        self, *, now_ns: int, cloud_now_timeout_ns: int
    ) -> tuple[BearingDecisionResult, ...]:
        if cloud_now_timeout_ns <= 0:
            raise ValueError("cloud_now_timeout_ns must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM bearing_decision_result WHERE is_current=1"
            ).fetchall()
        return tuple(
            result
            for row in rows
            if (result := _deserialize(str(row["payload_json"]))).lifecycle_state
            is BearingLifecycleStatus.WAITING_CLOUD
            and result.edge_accepted_at_ns + cloud_now_timeout_ns <= now_ns
        )

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


def _serialize(result: BearingDecisionResult) -> str:
    payload = asdict(result)
    payload["lifecycle_state"] = result.lifecycle_state.value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _deserialize(payload_json: str) -> BearingDecisionResult:
    payload = json.loads(payload_json)
    payload["lifecycle_state"] = BearingLifecycleStatus(payload["lifecycle_state"])
    return BearingDecisionResult(**payload)
