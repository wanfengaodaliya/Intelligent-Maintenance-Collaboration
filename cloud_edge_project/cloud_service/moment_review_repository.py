from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect


class MomentReviewRepository:
    """Persist and query cloud-side MOMENT packet reviews."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def save(self, result: dict[str, Any]) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO cloud_moment_review_record (
                    review_id, result_id, schema_version, device_id, task_id, bearing_id,
                    sender_id, decision_round_id, diagnosis_window_id,
                    window_start_sequence, window_end_sequence, window_start_ns, window_end_ns,
                    bearing_state, confidence, data_quality_score, risk_level,
                    action_grade, recommended_action, model_version, created_at_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(review_id) DO UPDATE SET
                    result_id=excluded.result_id,
                    bearing_state=excluded.bearing_state,
                    confidence=excluded.confidence,
                    risk_level=excluded.risk_level,
                    action_grade=excluded.action_grade,
                    recommended_action=excluded.recommended_action,
                    model_version=excluded.model_version,
                    created_at_ns=excluded.created_at_ns
                """,
                (
                    result["review_id"],
                    result["result_id"],
                    result["schema_version"],
                    result["device_id"],
                    result["task_id"],
                    result["bearing_id"],
                    result["sender_id"],
                    result["decision_round_id"],
                    result["diagnosis_window_id"],
                    result["window_start_sequence"],
                    result["window_end_sequence"],
                    result["window_start_ns"],
                    result["window_end_ns"],
                    result["bearing_state"],
                    result["confidence"],
                    result["data_quality_score"],
                    result["risk_level"],
                    result["action_grade"],
                    result["recommended_action"],
                    result["model_version"],
                    result["created_at_ns"],
                ),
            )

    def get(self, review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM cloud_moment_review_record WHERE review_id=?",
                (review_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_recent(
        self, device_id: str, task_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM cloud_moment_review_record "
                "WHERE device_id=? AND task_id=? ORDER BY created_at_ns DESC LIMIT ?",
                (device_id, task_id, min(limit, 500)),
            ).fetchall()
        return [dict(row) for row in rows]