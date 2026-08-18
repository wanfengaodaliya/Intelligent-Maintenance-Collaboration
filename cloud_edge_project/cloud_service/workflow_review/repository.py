from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class WorkflowReviewRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def create(self, review_id: str, review_type: str, request: dict[str, Any], status: str) -> dict[str, Any]:
        request_json = canonical_json(request)
        fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO workflow_review_job(review_id,review_type,status,request_sha256,request_json,created_at_ns,updated_at_ns) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(review_id) DO NOTHING",
                (review_id, review_type, status, fingerprint, request_json, now, now),
            )
            row = connection.execute(
                "SELECT * FROM workflow_review_job WHERE review_id=?", (review_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("workflow review was not persisted")
        if row["review_type"] != review_type or row["request_sha256"] != fingerprint:
            raise ValueError("REVIEW_ID_CONFLICT")
        return _row(row)

    def get(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_review_job WHERE review_id=?", (review_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def store_raw(self, review_id: str, raw_packets: list[dict[str, Any]]) -> dict[str, Any]:
        raw_json = canonical_json(raw_packets)
        now = time.time_ns()
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT status,raw_batch_json FROM workflow_review_job WHERE review_id=?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError("REVIEW_NOT_FOUND")
            if row["raw_batch_json"] is not None and row["raw_batch_json"] != raw_json:
                raise ValueError("RAW_BATCH_CONFLICT")
            if not (
                row["raw_batch_json"] == raw_json
                and row["status"] in {"RUNNING", "SUCCEEDED"}
            ):
                connection.execute(
                    "UPDATE workflow_review_job SET raw_batch_json=?,status='PENDING',updated_at_ns=? WHERE review_id=?",
                    (raw_json, now, review_id),
                )
        return self.get(review_id)  # type: ignore[return-value]

    def pending_ids(self, limit: int = 20) -> list[str]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT review_id FROM workflow_review_job WHERE status='PENDING' "
                "ORDER BY created_at_ns LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["review_id"] for row in rows]

    def mark_running(self, review_id: str) -> bool:
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE workflow_review_job SET status='RUNNING',updated_at_ns=? "
                "WHERE review_id=? AND status='PENDING'",
                (time.time_ns(), review_id),
            )
        return cursor.rowcount == 1

    def succeed(self, review_id: str, result: dict[str, Any]) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE workflow_review_job SET status='SUCCEEDED',result_json=?,error_code=NULL,updated_at_ns=? WHERE review_id=?",
                (canonical_json(result), time.time_ns(), review_id),
            )

    def fail(self, review_id: str, error_code: str) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE workflow_review_job SET status='FAILED',error_code=?,updated_at_ns=? WHERE review_id=?",
                (error_code, time.time_ns(), review_id),
            )


def _row(row: Any) -> dict[str, Any]:
    return {
        "review_id": row["review_id"],
        "review_type": row["review_type"],
        "status": row["status"],
        "request": json.loads(row["request_json"]),
        "raw_packets": json.loads(row["raw_batch_json"]) if row["raw_batch_json"] else None,
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error_code": row["error_code"],
        "created_at_ns": row["created_at_ns"],
        "updated_at_ns": row["updated_at_ns"],
    }
