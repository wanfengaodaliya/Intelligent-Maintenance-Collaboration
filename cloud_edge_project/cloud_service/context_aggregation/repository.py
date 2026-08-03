from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from cloud_service.storage.database import connect


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ContextAggregationRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def create_or_get(self, review_id: str, fingerprint: str, config_version: str, context_status: str) -> dict:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM aggregation_result WHERE review_id=? AND source_fingerprint=? AND preprocessing_config_version=?",
                (review_id, fingerprint, config_version),
            ).fetchone()
            if row:
                existing = dict(row)
                if (
                    existing["aggregation_status"] == "failed"
                    and existing["retryable"]
                    and existing["next_retry_at_ns"] is not None
                    and existing["next_retry_at_ns"] <= now
                ):
                    connection.execute(
                        "UPDATE aggregation_result SET aggregation_status='queued',lease_until_ns=NULL,next_retry_at_ns=NULL,updated_at_ns=? WHERE aggregation_id=?",
                        (now, existing["aggregation_id"]),
                    )
                    existing["aggregation_status"] = "queued"
                return existing
            aggregation_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO aggregation_result(aggregation_id,review_id,source_fingerprint,preprocessing_config_version,aggregation_status,context_status,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?,?)",
                (aggregation_id, review_id, fingerprint, config_version, "queued", context_status, now, now),
            )
            return dict(connection.execute("SELECT * FROM aggregation_result WHERE aggregation_id=?", (aggregation_id,)).fetchone())

    def acquire_lease(self, aggregation_id: str) -> bool:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            result = connection.execute(
                "UPDATE aggregation_result SET aggregation_status='running', lease_until_ns=?, attempt_count=attempt_count+1, updated_at_ns=? WHERE aggregation_id=? AND aggregation_status='queued'",
                (now + 60_000_000_000, now, aggregation_id),
            )
        return result.rowcount == 1

    def mark_succeeded(self, aggregation_id: str, metadata: dict) -> dict:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE aggregation_result SET aggregation_status='succeeded',relative_positions_json=?,packet_manifest_json=?,packet_boundaries_json=?,raw_window_path=?,raw_window_sha256=?,preprocessed_window_path=?,preprocessed_window_sha256=?,sample_counts_json=?,quality_summary_json=?,lease_until_ns=NULL,error_code=NULL,error_detail=NULL,updated_at_ns=?,succeeded_at_ns=? WHERE aggregation_id=?",
                (_json(metadata["relative_positions"]), _json(metadata["manifest"]), _json(metadata["boundaries"]), metadata["raw_path"], metadata["raw_sha"], metadata["processed_path"], metadata["processed_sha"], _json(metadata["sample_counts"]), _json({}), now, now, aggregation_id),
            )
            payload = _json(metadata["event"])
            connection.execute(
                "INSERT OR IGNORE INTO aggregation_outbox(outbox_id,aggregation_id,event_type,payload_json,dispatch_status,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), aggregation_id, "preprocessed_window_ready", payload, "pending", now, now),
            )
            return dict(connection.execute("SELECT * FROM aggregation_result WHERE aggregation_id=?", (aggregation_id,)).fetchone())

    def mark_failed(self, aggregation_id: str, code: str, detail: str, *, retryable: bool) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT attempt_count FROM aggregation_result WHERE aggregation_id=?", (aggregation_id,)).fetchone()
            attempts = row["attempt_count"] if row else 5
            can_retry = retryable and attempts < 5
            retry_at = now + min(5 * 2 ** max(attempts - 1, 0), 300) * 1_000_000_000 if can_retry else None
            connection.execute(
                "UPDATE aggregation_result SET aggregation_status='failed', retryable=?,next_retry_at_ns=?,lease_until_ns=NULL,error_code=?,error_detail=?,updated_at_ns=? WHERE aggregation_id=?",
                (int(can_retry), retry_at, code, detail, now, aggregation_id),
            )

    def get(self, aggregation_id: str) -> dict:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM aggregation_result WHERE aggregation_id=?", (aggregation_id,)).fetchone()
        return dict(row)

    def eligible_review_ids(self, limit: int) -> list[str]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT review_id FROM raw_context_request WHERE request_status IN ('complete','partial_context') ORDER BY updated_at_ns LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["review_id"] for row in rows]

    def recover_expired_leases(self) -> int:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            result = connection.execute(
                "UPDATE aggregation_result SET aggregation_status='queued',lease_until_ns=NULL,error_code='LEASE_EXPIRED',updated_at_ns=? WHERE aggregation_status='running' AND lease_until_ns < ?",
                (now, now),
            )
        return result.rowcount
