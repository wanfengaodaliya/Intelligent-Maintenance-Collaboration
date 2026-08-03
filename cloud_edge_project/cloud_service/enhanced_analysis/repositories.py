"""SQLite persistence for enhanced-analysis results and device metadata."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect

from .config import AnalysisConfig
from .contracts import EnhancedAnalysisError, EnhancedAnalysisResult


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class EnhancedAnalysisRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def review_exists(self, review_id: str) -> bool:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT 1 FROM cloud_review WHERE review_id=?", (review_id,)
            ).fetchone()
        return row is not None

    def find_aggregation(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM aggregation_result WHERE review_id=? AND aggregation_status='succeeded' "
                "ORDER BY updated_at_ns DESC LIMIT 1",
                (review_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_succeeded(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM enhanced_analysis_result WHERE review_id=? AND status='succeeded' LIMIT 1",
                (review_id,),
            ).fetchone()
        return dict(row) if row else None

    def start(
        self, review_id: str, aggregation: dict[str, Any], config: AnalysisConfig
    ) -> tuple[dict[str, Any], bool]:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            succeeded = connection.execute(
                "SELECT * FROM enhanced_analysis_result WHERE review_id=? AND status='succeeded' LIMIT 1",
                (review_id,),
            ).fetchone()
            if succeeded:
                return dict(succeeded), False
            row = connection.execute(
                "SELECT * FROM enhanced_analysis_result WHERE review_id=?", (review_id,)
            ).fetchone()
            if row:
                existing = dict(row)
                if existing["status"] == "running":
                    return existing, False
                if existing["status"] == "failed" and (
                    not existing["retryable"]
                    or (
                        existing["next_retry_at_ns"] is not None
                        and existing["next_retry_at_ns"] > now
                    )
                ):
                    raise EnhancedAnalysisError(
                        existing["error_code"] or "RESULT_NOT_RETRYABLE",
                        existing["error_detail"] or "analysis result is not retryable",
                        retryable=False,
                    )
                connection.execute(
                    "UPDATE enhanced_analysis_result SET status='running',retryable=1,next_retry_at_ns=NULL,"
                    "error_code=NULL,error_detail=NULL,attempt_count=attempt_count+1,updated_at_ns=? "
                    "WHERE review_id=?",
                    (now, review_id),
                )
                updated = dict(
                    connection.execute(
                        "SELECT * FROM enhanced_analysis_result WHERE review_id=?", (review_id,)
                    ).fetchone()
                )
                return updated, True

            analysis_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO enhanced_analysis_result("
                "analysis_id,review_id,status,context_status,algorithm_version,config_version,"
                "aggregation_id,limitations_json,retryable,attempt_count,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,?,'[]',1,1,?,?)",
                (
                    analysis_id,
                    review_id,
                    "running",
                    aggregation["context_status"],
                    config.algorithm_version,
                    config.config_version,
                    aggregation["aggregation_id"],
                    now,
                    now,
                ),
            )
            created = dict(
                connection.execute(
                    "SELECT * FROM enhanced_analysis_result WHERE analysis_id=?", (analysis_id,)
                ).fetchone()
            )
            return created, True

    def complete(self, result: EnhancedAnalysisResult) -> dict[str, Any]:
        now = time.time_ns()
        result_json = _json(result.to_dict())
        limitations_json = _json(result.limitations)
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE enhanced_analysis_result SET status='succeeded',result_json=?,limitations_json=?,"
                "retryable=0,next_retry_at_ns=NULL,error_code=NULL,error_detail=NULL,updated_at_ns=? "
                "WHERE review_id=?",
                (result_json, limitations_json, now, result.review_id),
            )
            analysis_row = connection.execute(
                "SELECT analysis_id FROM enhanced_analysis_result WHERE review_id=?",
                (result.review_id,),
            ).fetchone()
            analysis_id = analysis_row["analysis_id"]
            connection.execute(
                "INSERT OR IGNORE INTO enhanced_analysis_outbox("
                "outbox_id,analysis_id,event_type,review_id,status,error_code,created_at_ns,updated_at_ns"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    analysis_id,
                    "enhanced_analysis.succeeded",
                    result.review_id,
                    "succeeded",
                    None,
                    now,
                    now,
                ),
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM enhanced_analysis_result WHERE review_id=?", (result.review_id,)
                ).fetchone()
            )
        return row

    def fail(self, review_id: str, code: str, detail: str, *, retryable: bool) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt_count FROM enhanced_analysis_result WHERE review_id=?", (review_id,)
            ).fetchone()
            attempts = (row["attempt_count"] if row else 0) + 1
            can_retry = retryable and attempts < 5
            retry_at = (
                now + min(5 * 2 ** max(attempts - 1, 0), 300) * 1_000_000_000
                if can_retry
                else None
            )
            if row:
                connection.execute(
                    "UPDATE enhanced_analysis_result SET status='failed',retryable=?,next_retry_at_ns=?,"
                    "attempt_count=?,error_code=?,error_detail=?,updated_at_ns=? WHERE review_id=?",
                    (int(can_retry), retry_at, attempts, code, detail, now, review_id),
                )

    def result_from_row(self, row: dict[str, Any]) -> EnhancedAnalysisResult:
        value = _load_json(row["result_json"])
        return EnhancedAnalysisResult.from_dict(value)

    def enhanced_history(self, review_id: str, sender_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT er.review_id,er.result_json,er.created_at_ns FROM enhanced_analysis_result er "
                "JOIN cloud_review cr ON cr.review_id=er.review_id "
                "WHERE cr.sender_id=? AND er.status='succeeded' AND er.review_id<>? "
                "ORDER BY er.created_at_ns",
                (sender_id, review_id),
            ).fetchall()
        return [
            {
                "review_id": row["review_id"],
                "created_at_ns": row["created_at_ns"],
                "result": _load_json(row["result_json"]),
            }
            for row in rows
        ]

    def edge_history(self, sender_id: str, now_ns: int, lookback_days: int) -> list[dict[str, Any]]:
        since_ns = now_ns - lookback_days * 86_400 * 1_000_000_000
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM edge_packet_summary WHERE sender_id=? AND processing_status='perception_completed' "
                "AND end_timestamp_ns>=? ORDER BY end_timestamp_ns DESC LIMIT 1000",
                (sender_id, since_ns),
            ).fetchall()
        return [dict(row) for row in rows]


class BearingMetadataRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def active_for_sender_at(self, sender_id: str, timestamp_ns: int) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM bearing_configuration WHERE sender_id=? AND active=1 "
                "AND effective_from_ns<=? AND (effective_to_ns IS NULL OR effective_to_ns>?) "
                "ORDER BY effective_from_ns DESC LIMIT 2",
                (sender_id, timestamp_ns, timestamp_ns),
            ).fetchall()
        if len(rows) > 1:
            raise EnhancedAnalysisError(
                "BEARING_METADATA_INVALID", "multiple active bearing configurations apply", retryable=False
            )
        return dict(rows[0]) if rows else None
