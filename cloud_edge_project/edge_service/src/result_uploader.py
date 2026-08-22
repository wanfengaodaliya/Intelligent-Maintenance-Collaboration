"""Durable idempotent reporting of V1.2 bearing and device revisions."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from typing import Any, Callable, Mapping

from core.diagnosis_contracts import BearingDecisionResult, DeviceDecisionResult


PENDING = "PENDING"
UPLOADING = "UPLOADING"
ACKNOWLEDGED = "ACKNOWLEDGED"
CONFLICT = "CONFLICT"
DEAD_LETTER = "DEAD_LETTER"


class ResultUploader:
    def __init__(
        self, database_path, post: Callable[[str, dict], Mapping[str, object]],
        *,
        max_backoff_seconds: int = 300,
        max_attempts: int = 8,
        payload_enricher: Callable[[Mapping[str, Any], str], Mapping[str, Any]] | None = None,
    ) -> None:
        if max_backoff_seconds <= 0:
            raise ValueError("max_backoff_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.database_path = str(database_path)
        self.post = post
        self.max_backoff_seconds = max_backoff_seconds
        # 阶段 5：重试上限，超过后进入死信等待人工恢复，禁止无限重试。
        self.max_attempts = max_attempts
        # 阶段 4：出站上报载荷统一附加 trace 身份字段（route_id 为上报路径）。
        self._payload_enricher = payload_enricher
        self._initialize()

    def enqueue_bearing(self, result: BearingDecisionResult) -> None:
        value = asdict(result)
        value["lifecycle_state"] = result.lifecycle_state.value
        self._enqueue("/cloud/bearing-diagnosis-results", value)

    def enqueue_device(
        self,
        result: DeviceDecisionResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        return self._enqueue(
            "/cloud/device-decision-results",
            result.as_dict(),
            connection=connection,
        )

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
            retryable = bool(getattr(error, "retryable", True))
            if not retryable or attempts >= self.max_attempts:
                # 契约、路由等不可恢复错误无需等待；只对显式可重试的暂态
                # 错误使用重试预算。两类失败都保留在死信中供人工恢复。
                with self._connect() as connection:
                    connection.execute(
                        """UPDATE v12_result_upload
                        SET status=?,attempt_count=?,last_error=? WHERE result_id=?""",
                        (DEAD_LETTER, attempts, f"{type(error).__name__}: {error}", row["result_id"]),
                    )
                return 1
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

    def health(self) -> dict[str, Any]:
        """阶段 5：上报队列指标——状态计数、积压与最老记录年龄。"""
        now = time.time_ns()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM v12_result_upload GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                """SELECT MIN(created_at_ns) AS oldest_ns FROM v12_result_upload
                WHERE status IN (?, ?)""",
                (PENDING, UPLOADING),
            ).fetchone()
        counts: dict[str, Any] = {
            status: 0 for status in (PENDING, UPLOADING, ACKNOWLEDGED, CONFLICT, DEAD_LETTER)
        }
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        counts["backlog"] = counts[PENDING] + counts[UPLOADING]
        oldest_ns = oldest["oldest_ns"] if oldest is not None else None
        counts["oldest_backlog_age_ms"] = (
            None if oldest_ns is None else max((now - int(oldest_ns)) / 1_000_000.0, 0.0)
        )
        return counts

    def requeue_dead_letter(self, result_id: str) -> bool:
        """人工恢复入口：将死信记录重置为待上传（attempt 清零）。"""
        with self._connect() as connection:
            return connection.execute(
                """UPDATE v12_result_upload
                SET status='PENDING',attempt_count=0,next_attempt_at_ns=NULL
                WHERE result_id=? AND status=?""",
                (result_id, DEAD_LETTER),
            ).rowcount == 1

    def _enqueue(
        self,
        path: str,
        payload: dict,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        if self._payload_enricher is not None:
            payload = dict(self._payload_enricher(payload, path))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            existing = selected.execute("SELECT payload_json FROM v12_result_upload WHERE result_id=?", (payload["result_id"],)).fetchone()
            if existing is None:
                selected.execute(
                    """INSERT INTO v12_result_upload(
                    result_id,path,payload_json,status,created_at_ns,attempt_count,next_attempt_at_ns,last_error
                    ) VALUES (?,?,?,?,?,0,NULL,NULL)""",
                    (payload["result_id"], path, encoded, "PENDING", payload["created_at_ns"]),
                )
                return True
            elif existing["payload_json"] != encoded:
                selected.execute("UPDATE v12_result_upload SET status='CONFLICT' WHERE result_id=?", (payload["result_id"],))
                return False
            return True

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

    @contextmanager
    def _connect(self):
        # AUD-09: commit on success, rollback on error, and always close.
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
