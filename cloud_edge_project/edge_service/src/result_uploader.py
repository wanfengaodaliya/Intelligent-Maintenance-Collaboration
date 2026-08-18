"""Durable idempotent reporting of V1.2 bearing and device revisions."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import Callable, Mapping

from core.diagnosis_contracts import BearingDecisionResult, DeviceDecisionResult


class ResultUploader:
    def __init__(
        self, database_path, post: Callable[[str, dict], Mapping[str, object]],
        *, max_backoff_seconds: int = 300,
    ) -> None:
        if max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds must be positive")
        self.database_path = str(database_path)
        self.post = post
        self.max_backoff_seconds = max_backoff_seconds
        self._initialize()

    def enqueue_bearing(self, result: BearingDecisionResult) -> None:
        value = asdict(result)
        value["lifecycle_state"] = result.lifecycle_state.value
        self._enqueue("/cloud/bearing-diagnosis-results", value)

    def enqueue_device(self, result: DeviceDecisionResult) -> None:
        self._enqueue("/cloud/device-decision-results", result.as_dict())

    def run_once(self, now_ns: int | None = None) -> int:
        now = time.time_ns() if now_ns is None else now_ns
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM v12_result_upload
                WHERE status='PENDING' AND (next_attempt_at_ns IS NULL OR next_attempt_at_ns<=?)
                ORDER BY created_at_ns,result_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return 0
            connection.execute("UPDATE v12_result_upload SET status='UPLOADING' WHERE result_id=?", (row["result_id"],))
        try:
            response = self.post(row["path"], json.loads(row["payload_json"]))
            status = response.get("status")
            with self._connect() as connection:
                connection.execute(
                    """UPDATE v12_result_upload
                    SET status=?,next_attempt_at_ns=NULL,last_error=NULL WHERE result_id=?""",
                    (
                        "ACKNOWLEDGED" if status in {"accepted", "duplicate"} else "CONFLICT",
                        row["result_id"],
                    ),
                )
        except Exception as error:
            attempts = int(row["attempt_count"]) + 1
            delay_seconds = min(2 ** (attempts - 1), self.max_backoff_seconds)
            with self._connect() as connection:
                connection.execute(
                    """UPDATE v12_result_upload
                    SET status='PENDING',attempt_count=?,next_attempt_at_ns=?,last_error=?
                    WHERE result_id=?""",
                    (
                        attempts, now + delay_seconds * 1_000_000_000,
                        f"{type(error).__name__}: {error}", row["result_id"],
                    ),
                )
        return 1

    def _enqueue(self, path: str, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute("SELECT payload_json FROM v12_result_upload WHERE result_id=?", (payload["result_id"],)).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO v12_result_upload(
                    result_id,path,payload_json,status,created_at_ns,attempt_count,next_attempt_at_ns,last_error
                    ) VALUES (?,?,?,?,?,0,NULL,NULL)""",
                    (payload["result_id"], path, encoded, "PENDING", payload["created_at_ns"]),
                )
            elif existing["payload_json"] != encoded:
                connection.execute("UPDATE v12_result_upload SET status='CONFLICT' WHERE result_id=?", (payload["result_id"],))

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS v12_result_upload(
                result_id TEXT PRIMARY KEY,path TEXT NOT NULL,payload_json TEXT NOT NULL,
                status TEXT NOT NULL,created_at_ns INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,next_attempt_at_ns INTEGER,last_error TEXT)""")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(v12_result_upload)")
            }
            for name, definition in (
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("next_attempt_at_ns", "INTEGER"),
                ("last_error", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE v12_result_upload ADD COLUMN {name} {definition}"
                    )
            connection.execute("UPDATE v12_result_upload SET status='PENDING' WHERE status='UPLOADING'")

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
