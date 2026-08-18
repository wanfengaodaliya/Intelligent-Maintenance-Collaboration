"""Persistence for cloud review state and its raw-packet context."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .database import connect


def _json(value: object | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CloudReviewRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def upsert_preliminary(self, *, device_id: str, bearing_id: str, sender_id: str, anchor_packet_id: str, task_id: str,
                           feature_extractor_version: str, schema_version: str,
                           data_quality_valid: bool, data_quality: dict,
                           start_timestamp_ns: int | None = None,
                           end_timestamp_ns: int | None = None) -> str:
        """Create the preliminary review once and return its stable identifier."""
        now = time.time_ns()
        with connect(self.database_path) as connection:
            existing = connection.execute(
                "SELECT review_id,device_id,task_id,bearing_id FROM cloud_review "
                "WHERE sender_id=? AND anchor_packet_id=? AND feature_extractor_version=?",
                (sender_id, anchor_packet_id, feature_extractor_version),
            ).fetchone()
            if existing:
                if (
                    existing["device_id"] != device_id
                    or existing["task_id"] != task_id
                    or existing["bearing_id"] != bearing_id
                ):
                    raise ValueError("REVIEW_IDENTITY_CONFLICT")
                return existing["review_id"]
            review_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO cloud_review(review_id,sender_id,anchor_packet_id,device_id,task_id,bearing_id,feature_extractor_version,schema_version,review_status,context_status,data_quality_valid,start_timestamp_ns,end_timestamp_ns,data_quality_json,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, sender_id, anchor_packet_id, device_id, task_id, bearing_id, feature_extractor_version, schema_version,
                 "preliminary", "pending_context", int(data_quality_valid), start_timestamp_ns,
                 end_timestamp_ns, _json(data_quality), now, now),
            )
        return review_id

    def complete(self, review_id: str, *, cloud_recomputed_features: dict,
                 cloud_enhanced_features: dict | None = None, advanced_features: dict | None = None,
                 context_features: dict | None = None, packet_count: int = 1) -> None:
        if packet_count < 1:
            raise ValueError("packet_count must be positive")
        with connect(self.database_path) as connection:
            result = connection.execute(
                "UPDATE cloud_review SET review_status='complete', context_status='complete', packet_count=?, cloud_recomputed_features_json=?, cloud_enhanced_features_json=?, advanced_features_json=?, context_features_json=?, updated_at_ns=? WHERE review_id=?",
                (packet_count, _json(cloud_recomputed_features), _json(cloud_enhanced_features),
                 _json(advanced_features), _json(context_features), time.time_ns(), review_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown review_id: {review_id}")

    def complete_packet_review(
        self, review_id: str, *, cloud_recomputed_features: dict
    ) -> None:
        """Mark an independent packet review complete without any context window."""

        with connect(self.database_path) as connection:
            result = connection.execute(
                "UPDATE cloud_review SET review_status='complete', context_status='not_requested', "
                "packet_count=1, cloud_recomputed_features_json=?, "
                "cloud_enhanced_features_json=NULL, advanced_features_json=NULL, "
                "context_features_json=NULL, updated_at_ns=? WHERE review_id=?",
                (_json(cloud_recomputed_features), time.time_ns(), review_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown review_id: {review_id}")

    def mark_insufficient_context(self, review_id: str) -> None:
        with connect(self.database_path) as connection:
            result = connection.execute(
                "UPDATE cloud_review SET review_status='insufficient_context', context_status='insufficient_context', updated_at_ns=? WHERE review_id=?",
                (time.time_ns(), review_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown review_id: {review_id}")

    def add_context_packet(self, review_id: str, sender_id: str, packet_id: str,
                           relative_position: int, role: str) -> None:
        if role not in {"before", "anchor", "after"}:
            raise ValueError("role must be before, anchor, or after")
        with connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO review_context_packets(review_id,sender_id,packet_id,relative_position,role) VALUES (?,?,?,?,?) "
                "ON CONFLICT(review_id,sender_id,packet_id) DO UPDATE SET relative_position=excluded.relative_position,role=excluded.role",
                (review_id, sender_id, packet_id, relative_position, role),
            )

    def context_positions(self, review_id: str) -> set[int]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT relative_position FROM review_context_packets "
                "WHERE review_id=?",
                (review_id,),
            ).fetchall()
        return {row["relative_position"] for row in rows}

    def get(self, review_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM cloud_review WHERE review_id=?", (review_id,)).fetchone()
        return dict(row) if row else None

    def context_packets(self, review_id: str) -> list[dict]:
        with connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM review_context_packets WHERE review_id=? ORDER BY relative_position", (review_id,)).fetchall()
        return [dict(row) for row in rows]
