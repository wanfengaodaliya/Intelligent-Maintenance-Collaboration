from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any

from .repository import SummaryRepository


class PermanentDeliveryError(RuntimeError):
    pass


Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ArbitrationOutbox:
    def __init__(
        self,
        repository: SummaryRepository,
        transport: Transport,
        *,
        now_ns: Callable[[], int] = time.time_ns,
        retry_delay_ns: int = 1_000_000_000,
        max_attempts: int = 1000,
        max_retry_delay_ns: int = 5_000_000_000,
        table_name: str = "summary_arbitration_outbox",
    ) -> None:
        if table_name not in {
            "summary_arbitration_outbox",
            "summary_window_publish_outbox",
            "summary_window_sync_outbox",
        }:
            raise ValueError("unsupported delivery outbox table")
        self.repository = repository
        self.transport = transport
        self.now_ns = now_ns
        self.retry_delay_ns = int(retry_delay_ns)
        self.max_attempts = int(max_attempts)
        self.max_retry_delay_ns = int(max_retry_delay_ns)
        self.table_name = table_name
        self._recover_interrupted_deliveries()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.repository.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _recover_interrupted_deliveries(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE {self.table_name}
                SET state = 'RETRY_WAIT', next_attempt_at_ns = 0,
                    last_error = COALESCE(last_error, 'delivery interrupted before acknowledgement')
                WHERE state = 'UPLOADING'
                """
            )

    def run_due(self, *, limit: int = 20) -> int:
        delivered = 0
        now = self.now_ns()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT request_id, payload_json, attempts
                FROM {self.table_name}
                WHERE state IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at_ns <= ?
                ORDER BY created_at_ns, request_id
                LIMIT ?
                """,
                (now, int(limit)),
            ).fetchall()

        for row in rows:
            request_id = str(row["request_id"])
            attempts = int(row["attempts"]) + 1
            with self._connect() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET state = 'UPLOADING', attempts = ?
                    WHERE request_id = ? AND state IN ('PENDING', 'RETRY_WAIT')
                    """,
                    (attempts, request_id),
                )
            if cursor.rowcount != 1:
                continue

            try:
                response = dict(self.transport(json.loads(row["payload_json"])))
            except PermanentDeliveryError as exc:
                self._mark_failed(request_id, attempts, str(exc), permanent=True)
            except Exception as exc:
                self._mark_failed(
                    request_id,
                    attempts,
                    str(exc),
                    permanent=attempts >= self.max_attempts,
                )
            else:
                self._acknowledge(request_id, response)
                delivered += 1
        return delivered

    def _acknowledge(self, request_id: str, response: Mapping[str, Any]) -> None:
        now = self.now_ns()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE {self.table_name}
                SET state = 'ACKNOWLEDGED', acknowledged_at_ns = ?,
                    cloud_result_json = ?, last_error = NULL
                WHERE request_id = ?
                """,
                (now, json.dumps(response, ensure_ascii=False, sort_keys=True), request_id),
            )

    def _mark_failed(
        self, request_id: str, attempts: int, error: str, *, permanent: bool
    ) -> None:
        delay = min(
            self.retry_delay_ns * max(1, attempts), self.max_retry_delay_ns
        )
        next_attempt = self.now_ns() + delay
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE {self.table_name}
                SET state = ?, next_attempt_at_ns = ?, last_error = ?
                WHERE request_id = ?
                """,
                (
                    "DEAD_LETTER" if permanent else "RETRY_WAIT",
                    next_attempt,
                    error[:1000],
                    request_id,
                ),
            )
