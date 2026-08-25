# -*- coding: utf-8 -*-
"""Durable publish outbox for local bearing and device decision results."""
# 该模块以先落库后发送的方式维护本地结果的发布生命周期。

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from typing import Any, Callable, Mapping

from core.diagnosis_contracts import DeviceDecisionResult, EdgeBearingResult


PUBLISH_PENDING = "PUBLISH_PENDING"
PUBLISHING = "PUBLISHING"
PUBLISHED = "PUBLISHED"
RETRY_WAIT = "RETRY_WAIT"
DEAD_LETTER = "DEAD_LETTER"

_RETRYABLE_STATUSES = (PUBLISH_PENDING, RETRY_WAIT)
_BACKOFF_STEP_NS = 200_000_000
_MAX_BACKOFF_NS = 5_000_000_000


class DeviceResultOutbox:
    """Persist a result before publishing; the same identity publishes once."""

    def __init__(
        self,
        database_path,
        publisher: Callable[[Mapping[str, Any]], Any],
        *,
        max_attempts: int = 5,
        clock_ns: Callable[[], int] = time.time_ns,
        namespace: str = "device_result",
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if namespace not in {"device_result", "bearing_result"}:
            raise ValueError("unsupported outbox namespace")
        self.database_path = str(database_path)
        self.publisher = publisher
        self.max_attempts = max_attempts
        self.clock_ns = clock_ns
        self.outbox_table = f"{namespace}_outbox"
        self.history_table = f"{namespace}_delivery_history"
        self._initialize()

    def enqueue(
        self,
        result: DeviceDecisionResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Idempotent insert keyed by device_result_id + revision."""
        return self._enqueue_payload(
            result.as_dict(),
            result_id=result.result_id,
            device_id=result.device_id,
            task_id=result.task_id,
            decision_round_id=result.decision_round_id,
            revision=result.revision,
            connection=connection,
        )

    def enqueue_bearing(
        self,
        result: EdgeBearingResult,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Idempotently enqueue one immutable local bearing decision."""
        return self._enqueue_payload(
            asdict(result),
            result_id=result.result_id,
            device_id=result.device_id,
            task_id=result.task_id,
            decision_round_id=result.decision_round_id,
            revision=1,
            connection=connection,
        )

    def _enqueue_payload(
        self,
        value: Mapping[str, Any],
        *,
        result_id: str,
        device_id: str,
        task_id: str,
        decision_round_id: str,
        revision: int,
        connection: sqlite3.Connection | None,
    ) -> bool:
        payload = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with (self._connect() if connection is None else nullcontext(connection)) as selected:
            existing = selected.execute(
                f"SELECT payload_json FROM {self.outbox_table} WHERE result_id=?",
                (result_id,),
            ).fetchone()
            if existing is not None:
                return existing["payload_json"] == payload
            delivered = selected.execute(
                f"""SELECT payload_json FROM {self.history_table}
                WHERE result_id=?""",
                (result_id,),
            ).fetchone()
            if delivered is not None:
                return delivered["payload_json"] == payload
            selected.execute(
                f"""INSERT INTO {self.outbox_table}(
                result_id, device_id, task_id, decision_round_id, revision,
                payload_json, status, attempt_count, next_attempt_at_ns,
                last_error, created_at_ns, published_at_ns
                ) VALUES (?,?,?,?,?,?,?,0,NULL,NULL,?,NULL)""",
                (
                    result_id,
                    device_id,
                    task_id,
                    decision_round_id,
                    revision,
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
                f"""SELECT result_id, payload_json, attempt_count FROM {self.outbox_table}
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
                f"SELECT status, COUNT(*) AS total FROM {self.outbox_table} GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                f"""SELECT MIN(created_at_ns) AS oldest_ns FROM {self.outbox_table}
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
            connection.execute(
                f"""INSERT OR IGNORE INTO {self.history_table}(
                result_id, payload_json, published_at_ns
                ) SELECT result_id, payload_json, published_at_ns
                FROM {self.outbox_table}
                WHERE status=? AND published_at_ns IS NOT NULL AND published_at_ns<?""",
                (PUBLISHED, cutoff),
            )
            cursor = connection.execute(
                f"DELETE FROM {self.outbox_table} WHERE status=? AND published_at_ns IS NOT NULL AND published_at_ns<?",
                (PUBLISHED, cutoff),
            )
            return cursor.rowcount

    def _publish_one(self, result_id: str, payload_json: str, attempt_count: int, now: int) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                f"""UPDATE {self.outbox_table} SET status=?
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
                    f"""UPDATE {self.outbox_table}
                    SET status=?, attempt_count=?, next_attempt_at_ns=?, last_error=?
                    WHERE result_id=?""",
                    (status, attempts, next_at, f"{type(error).__name__}: {error}", result_id),
                )
            return False
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE {self.outbox_table}
                SET status=?, attempt_count=?, next_attempt_at_ns=NULL,
                    last_error=NULL, published_at_ns=?
                WHERE result_id=?""",
                (PUBLISHED, attempt_count + 1, self.clock_ns(), result_id),
            )
        return True

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.outbox_table}(
                result_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, task_id TEXT NOT NULL,
                decision_round_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at_ns INTEGER,
                last_error TEXT, created_at_ns INTEGER NOT NULL, published_at_ns INTEGER)"""
            )
            # 启动恢复：进程在发送中途退出时，重新进入可重试状态，由后台再次发送。
            connection.execute(
                f"UPDATE {self.outbox_table} SET status=? WHERE status=?",
                (RETRY_WAIT, PUBLISHING),
            )
            connection.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_{self.outbox_table}_due
                ON {self.outbox_table}(status, next_attempt_at_ns)"""
            )
            # 清理已发布 Outbox 时保留轻量交付墓碑，避免周期对账把历史结果
            # 重新入队；payload_json 仍用于检测同 result_id 的载荷冲突。
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.history_table}(
                result_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                published_at_ns INTEGER NOT NULL)"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # AUD-09: commit on success, rollback on error, and always close.
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
