# -*- coding: utf-8 -*-
"""Durable publish outbox for device-level decision results."""
# 该模块以先落库后发送的方式维护设备级结果的发布生命周期。

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable, Mapping

from core.diagnosis_contracts import DeviceDecisionResult


PUBLISH_PENDING = "PUBLISH_PENDING"
PUBLISHING = "PUBLISHING"
PUBLISHED = "PUBLISHED"
RETRY_WAIT = "RETRY_WAIT"
DEAD_LETTER = "DEAD_LETTER"

_RETRYABLE_STATUSES = (PUBLISH_PENDING, RETRY_WAIT)
_BACKOFF_STEP_NS = 200_000_000
_MAX_BACKOFF_NS = 5_000_000_000


class DeviceResultOutbox:
    """Persist device results before publishing; the same version publishes once."""

    def __init__(
        self,
        database_path,
        publisher: Callable[[Mapping[str, Any]], Any],
        *,
        max_attempts: int = 5,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.database_path = str(database_path)
        self.publisher = publisher
        self.max_attempts = max_attempts
        self.clock_ns = clock_ns
        self._initialize()

    def enqueue(self, result: DeviceDecisionResult) -> bool:
        """Idempotent insert keyed by device_result_id + revision."""
        payload = json.dumps(
            result.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM device_result_outbox WHERE result_id=?",
                (result.result_id,),
            ).fetchone()
            if existing is not None:
                return existing["payload_json"] == payload
            connection.execute(
                """INSERT INTO device_result_outbox(
                result_id, device_id, task_id, decision_round_id, revision,
                payload_json, status, attempt_count, next_attempt_at_ns,
                last_error, created_at_ns, published_at_ns
                ) VALUES (?,?,?,?,?,?,?,0,NULL,NULL,?,NULL)""",
                (
                    result.result_id,
                    result.device_id,
                    result.task_id,
                    result.decision_round_id,
                    result.revision,
                    payload,
                    PUBLISH_PENDING,
                    self.clock_ns(),
                ),
            )
        return True

    def run_once(self, now_ns: int | None = None, *, limit: int = 16) -> int:
        """Publish due outbox entries; failures enter RETRY_WAIT or DEAD_LETTER."""
        now = self.clock_ns() if now_ns is None else now_ns
        published = 0
        with self._connect() as connection:
            due = connection.execute(
                """SELECT result_id, payload_json, attempt_count FROM device_result_outbox
                WHERE status IN (?, ?) AND (next_attempt_at_ns IS NULL OR next_attempt_at_ns<=?)
                ORDER BY created_at_ns, result_id LIMIT ?""",
                (PUBLISH_PENDING, RETRY_WAIT, now, limit),
            ).fetchall()
        for row in due:
            if self._publish_one(row["result_id"], row["payload_json"], int(row["attempt_count"]), now):
                published += 1
        return published

    def health(self) -> dict[str, Any]:
        now = self.clock_ns()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM device_result_outbox GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                """SELECT MIN(created_at_ns) AS oldest_ns FROM device_result_outbox
                WHERE status IN (?, ?, ?)""",
                (PUBLISH_PENDING, RETRY_WAIT, PUBLISHING),
            ).fetchone()
        counts: dict[str, Any] = {
            status: 0 for status in (PUBLISH_PENDING, PUBLISHING, PUBLISHED, RETRY_WAIT, DEAD_LETTER)
        }
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        counts["backlog"] = counts[PUBLISH_PENDING] + counts[RETRY_WAIT] + counts[PUBLISHING]
        oldest_ns = oldest["oldest_ns"] if oldest is not None else None
        counts["oldest_backlog_age_ms"] = (
            None if oldest_ns is None else max((now - int(oldest_ns)) / 1_000_000.0, 0.0)
        )
        return counts

    def cleanup_published(self, *, retention_ns: int, now_ns: int | None = None) -> int:
        """删除超过保留期的已发布记录；死信与未完成状态不做自动清理。

        阶段 5 数据保留策略：PUBLISHED 记录仅作审计回溯，超期即可删除；
        DEAD_LETTER 必须保留给人工恢复入口处理。
        """
        if retention_ns <= 0:
            return 0
        now = self.clock_ns() if now_ns is None else now_ns
        cutoff = now - retention_ns
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM device_result_outbox WHERE status=? AND published_at_ns IS NOT NULL AND published_at_ns<?",
                (PUBLISHED, cutoff),
            )
            return cursor.rowcount

    def _publish_one(self, result_id: str, payload_json: str, attempt_count: int, now: int) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE device_result_outbox SET status=?
                WHERE result_id=? AND status IN (?, ?)""",
                (PUBLISHING, result_id, PUBLISH_PENDING, RETRY_WAIT),
            ).rowcount
        if changed != 1:
            return False
        try:
            self.publisher(json.loads(payload_json))
        except Exception as error:
            attempts = attempt_count + 1
            if attempts >= self.max_attempts:
                status, next_at = DEAD_LETTER, None
            else:
                status = RETRY_WAIT
                backoff = min(_BACKOFF_STEP_NS * (2 ** (attempts - 1)), _MAX_BACKOFF_NS)
                next_at = now + backoff
            with self._connect() as connection:
                connection.execute(
                    """UPDATE device_result_outbox
                    SET status=?, attempt_count=?, next_attempt_at_ns=?, last_error=?
                    WHERE result_id=?""",
                    (status, attempts, next_at, f"{type(error).__name__}: {error}", result_id),
                )
            return False
        with self._connect() as connection:
            connection.execute(
                """UPDATE device_result_outbox
                SET status=?, attempt_count=?, next_attempt_at_ns=NULL,
                    last_error=NULL, published_at_ns=?
                WHERE result_id=?""",
                (PUBLISHED, attempt_count + 1, self.clock_ns(), result_id),
            )
        return True

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS device_result_outbox(
                result_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, task_id TEXT NOT NULL,
                decision_round_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at_ns INTEGER,
                last_error TEXT, created_at_ns INTEGER NOT NULL, published_at_ns INTEGER)"""
            )
            # 启动恢复：进程在发送中途退出时，重新进入可重试状态，由后台再次发送。
            connection.execute(
                "UPDATE device_result_outbox SET status=? WHERE status=?",
                (RETRY_WAIT, PUBLISHING),
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_device_result_outbox_due
                ON device_result_outbox(status, next_attempt_at_ns)"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
