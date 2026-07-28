"""Diagnosis-event repository with review-state guardrails."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .database import connect


class EventRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def create_pending(self, review_id: str, sender_id: str, packet_id: str,
                       diagnosis_model_version: str | None = None) -> str:
        """Create a diagnosis event only after the review has continuous context."""
        now = time.time_ns()
        with connect(self.database_path) as connection:
            review = connection.execute("SELECT review_status FROM cloud_review WHERE review_id=?", (review_id,)).fetchone()
            if review is None:
                raise KeyError(f"unknown review_id: {review_id}")
            if review["review_status"] != "complete":
                raise ValueError("diagnosis events require a complete review")
            event_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO diagnosis_events(event_id,review_id,sender_id,packet_id,diagnosis_model_version,diagnosis_status,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,'pending',?,?)",
                (event_id, review_id, sender_id, packet_id, diagnosis_model_version, now, now),
            )
        return event_id

    def update(self, event_id: str, status: str, *, result: dict | None = None,
               human_review: dict | None = None) -> None:
        if status not in {"pending", "completed", "skipped", "failed"}:
            raise ValueError("invalid diagnosis status")
        with connect(self.database_path) as connection:
            result_row = connection.execute(
                "UPDATE diagnosis_events SET diagnosis_status=?, result_json=?, human_review_json=?, updated_at_ns=? WHERE event_id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 json.dumps(human_review, ensure_ascii=False) if human_review is not None else None,
                 time.time_ns(), event_id),
            )
            if result_row.rowcount != 1:
                raise KeyError(f"unknown event_id: {event_id}")
