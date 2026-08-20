"""Durable, idempotent local queue for frozen raw samples."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .contracts import InsertOutcome, QueuedRawSample, RawAnalysisSample


class RawSampleRepository:
    def __init__(
        self, root: Path | str, *, max_storage_bytes: int | None = None,
        retention_ns: int | None = None,
        max_upload_attempts: int = 10,
    ) -> None:
        if max_storage_bytes is not None and max_storage_bytes <= 0:
            raise ValueError("max_storage_bytes must be positive")
        if retention_ns is not None and retention_ns <= 0:
            raise ValueError("retention_ns must be positive")
        if max_upload_attempts <= 0:
            raise ValueError("max_upload_attempts must be positive")
        self.root = Path(root)
        self.payload_directory = self.root / "payloads"
        self.payload_directory.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "raw_sample_queue.db"
        self.max_storage_bytes = max_storage_bytes
        self.retention_ns = retention_ns
        # 阶段 5：上传重试上限，超过后进入死信等待人工恢复，禁止无限重试。
        self.max_upload_attempts = max_upload_attempts
        self._initialize()
        self._recover_uploading()

    def enqueue(self, sample: RawAnalysisSample) -> InsertOutcome:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_sha256 FROM raw_sample_queue WHERE sample_id=?", (sample.sample_id,)
            ).fetchone()
            if row is not None:
                if row["payload_sha256"] == sample.payload_sha256:
                    return InsertOutcome("DUPLICATE", sample.sample_id)
                connection.execute(
                    "UPDATE raw_sample_queue SET status='CONFLICT' WHERE sample_id=?", (sample.sample_id,)
                )
                return InsertOutcome("CONFLICT", sample.sample_id)
            if not self._reserve_storage(connection, len(sample.payload)):
                connection.execute(
                    """INSERT INTO raw_sample_queue(
                    sample_id,payload_sha256,payload_path,metadata_json,status,attempt_count,
                    next_attempt_at_ns,last_error,created_at_ns
                    ) VALUES (?,?,?,?, 'EXPIRED', 0, NULL, 'LOCAL_STORAGE_QUOTA', ?)""",
                    (
                        sample.sample_id, sample.payload_sha256,
                        str(self.payload_directory / f"{sample.sample_id}.json"),
                        json.dumps(sample.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                        sample.created_at_ns,
                    ),
                )
                return InsertOutcome("EXPIRED", sample.sample_id)
            path = self._write_payload(sample)
            connection.execute(
                """INSERT INTO raw_sample_queue(
                sample_id,payload_sha256,payload_path,metadata_json,status,attempt_count,
                next_attempt_at_ns,last_error,created_at_ns
                ) VALUES (?,?,?,?, 'PENDING', 0, NULL, NULL, ?)""",
                (
                    sample.sample_id,
                    sample.payload_sha256,
                    str(path),
                    json.dumps(sample.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                    sample.created_at_ns,
                ),
            )
        return InsertOutcome("INSERTED", sample.sample_id)

    def get(self, sample_id: str) -> QueuedRawSample | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM raw_sample_queue WHERE sample_id=?", (sample_id,)
            ).fetchone()
        return None if row is None else _queued(row)

    def read_payload(self, sample_id: str) -> bytes | None:
        queued = self.get(sample_id)
        if queued is None:
            return None
        path = self.payload_directory / f"{sample_id}.json"
        return path.read_bytes() if path.exists() else None

    def mark_uploading(self, sample_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "UPDATE raw_sample_queue SET status='UPLOADING' WHERE sample_id=? AND status='PENDING'",
                (sample_id,),
            ).rowcount == 1

    def claim_due(self, *, now_ns: int, limit: int) -> tuple[QueuedRawSample, ...]:
        if self.retention_ns is not None:
            self.cleanup(now_ns=now_ns)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM raw_sample_queue AS candidate
                WHERE status='PENDING' AND (next_attempt_at_ns IS NULL OR next_attempt_at_ns<=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM raw_sample_queue AS earlier
                    WHERE earlier.status IN ('PENDING','UPLOADING')
                      AND json_extract(earlier.metadata_json, '$.device_id')=
                          json_extract(candidate.metadata_json, '$.device_id')
                      AND earlier.created_at_ns<candidate.created_at_ns
                  )
                ORDER BY created_at_ns, sample_id LIMIT ?""",
                (now_ns, limit),
            ).fetchall()
            claimed: list[QueuedRawSample] = []
            for row in rows:
                if connection.execute(
                    "UPDATE raw_sample_queue SET status='UPLOADING' WHERE sample_id=? AND status='PENDING'",
                    (row["sample_id"],),
                ).rowcount == 1:
                    claimed.append(_queued(row))
        return tuple(claimed)

    def cleanup(self, *, now_ns: int) -> int:
        if self.retention_ns is None:
            return 0
        cutoff = now_ns - self.retention_ns
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sample_id,payload_path FROM raw_sample_queue
                WHERE status IN ('PENDING','ACKNOWLEDGED','CONFLICT') AND created_at_ns<=?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE raw_sample_queue SET status='EXPIRED',next_attempt_at_ns=NULL WHERE sample_id=?",
                    (row["sample_id"],),
                )
        for row in rows:
            path = Path(row["payload_path"])
            if path.exists():
                path.unlink()
        return len(rows)

    def acknowledge(self, sample_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE raw_sample_queue SET status='ACKNOWLEDGED',next_attempt_at_ns=NULL,last_error=NULL WHERE sample_id=?",
                (sample_id,),
            )

    def mark_conflict(self, sample_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE raw_sample_queue SET status='CONFLICT' WHERE sample_id=?", (sample_id,))

    def retry(self, sample_id: str, *, now_ns: int, error: str, max_backoff_seconds: int) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM raw_sample_queue WHERE sample_id=?", (sample_id,)
            ).fetchone()
            attempts = int(row["attempt_count"]) + 1
            # 阶段 5：达到重试上限进入死信，保留现场等待人工恢复入口处理。
            if attempts >= self.max_upload_attempts:
                connection.execute(
                    """UPDATE raw_sample_queue
                    SET status='DEAD_LETTER',attempt_count=?,next_attempt_at_ns=NULL,last_error=?
                    WHERE sample_id=?""",
                    (attempts, error, sample_id),
                )
                return
            delay_seconds = min(2 ** (attempts - 1), max_backoff_seconds)
            connection.execute(
                """UPDATE raw_sample_queue
                SET status='PENDING',attempt_count=?,next_attempt_at_ns=?,last_error=?
                WHERE sample_id=?""",
                (attempts, now_ns + delay_seconds * 1_000_000_000, error, sample_id),
            )

    def requeue_dead_letter(self, sample_id: str) -> bool:
        """人工恢复入口：将死信样本重置为待上传（attempt 清零）。"""
        with self._connect() as connection:
            return connection.execute(
                """UPDATE raw_sample_queue
                SET status='PENDING',attempt_count=0,next_attempt_at_ns=NULL
                WHERE sample_id=? AND status='DEAD_LETTER'""",
                (sample_id,),
            ).rowcount == 1

    def health(self) -> dict[str, int]:
        """阶段 5：原始样本队列指标——状态计数与待上传积压。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM raw_sample_queue GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in (
            "PENDING", "UPLOADING", "ACKNOWLEDGED", "CONFLICT", "EXPIRED", "DEAD_LETTER",
        )}
        for row in rows:
            counts[str(row["status"])] = int(row["total"])
        counts["backlog"] = counts["PENDING"] + counts["UPLOADING"]
        return counts

    def _write_payload(self, sample: RawAnalysisSample) -> Path:
        target = self.payload_directory / f"{sample.sample_id}.json"
        temporary = self.payload_directory / f".{sample.sample_id}.tmp"
        with temporary.open("wb") as handle:
            handle.write(sample.payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def _reserve_storage(self, connection: sqlite3.Connection, incoming_bytes: int) -> bool:
        if self.max_storage_bytes is None:
            return True
        rows = connection.execute(
            "SELECT sample_id,payload_path,status FROM raw_sample_queue WHERE status!='EXPIRED' ORDER BY created_at_ns,sample_id"
        ).fetchall()
        used = sum(
            Path(row["payload_path"]).stat().st_size
            for row in rows if Path(row["payload_path"]).exists()
        )
        if used + incoming_bytes <= self.max_storage_bytes:
            return True
        for row in rows:
            if row["status"] != "ACKNOWLEDGED":
                continue
            path = Path(row["payload_path"])
            size = path.stat().st_size if path.exists() else 0
            connection.execute(
                "UPDATE raw_sample_queue SET status='EXPIRED' WHERE sample_id=?",
                (row["sample_id"],),
            )
            if path.exists():
                path.unlink()
            used -= size
            if used + incoming_bytes <= self.max_storage_bytes:
                return True
        return False

    def _recover_uploading(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE raw_sample_queue SET status='PENDING' WHERE status='UPLOADING'")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS raw_sample_queue(
                sample_id TEXT PRIMARY KEY,
                payload_sha256 TEXT NOT NULL,
                payload_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','UPLOADING','ACKNOWLEDGED','CONFLICT','EXPIRED','DEAD_LETTER')),
                attempt_count INTEGER NOT NULL,
                next_attempt_at_ns INTEGER,
                last_error TEXT,
                created_at_ns INTEGER NOT NULL
                )"""
            )
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_sample_queue'"
            ).fetchone()
            # 阶段 5：旧表 CHECK 不含 DEAD_LETTER，需重建迁移，数据原样保留。
            if schema is not None and "DEAD_LETTER" not in schema["sql"]:
                connection.execute("ALTER TABLE raw_sample_queue RENAME TO raw_sample_queue_legacy")
                connection.execute(
                    """CREATE TABLE raw_sample_queue(
                    sample_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    payload_path TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','UPLOADING','ACKNOWLEDGED','CONFLICT','EXPIRED','DEAD_LETTER')),
                    attempt_count INTEGER NOT NULL,
                    next_attempt_at_ns INTEGER,
                    last_error TEXT,
                    created_at_ns INTEGER NOT NULL
                    )"""
                )
                connection.execute(
                    """INSERT INTO raw_sample_queue
                    SELECT sample_id,payload_sha256,payload_path,metadata_json,status,
                           attempt_count,next_attempt_at_ns,last_error,created_at_ns
                    FROM raw_sample_queue_legacy"""
                )
                connection.execute("DROP TABLE raw_sample_queue_legacy")

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


def _queued(row: sqlite3.Row) -> QueuedRawSample:
    value = json.loads(row["metadata_json"])
    value.pop("schema_version", None)
    value["trigger_reasons"] = tuple(value["trigger_reasons"])
    value["packet_manifest"] = tuple(value["packet_manifest"])
    payload_path = Path(row["payload_path"])
    payload = payload_path.read_bytes() if payload_path.exists() else b""
    sample = RawAnalysisSample(payload=payload, **value)
    return QueuedRawSample(
        sample=sample,
        status=row["status"],
        attempt_count=row["attempt_count"],
        next_attempt_at_ns=row["next_attempt_at_ns"],
        last_error=row["last_error"],
    )
