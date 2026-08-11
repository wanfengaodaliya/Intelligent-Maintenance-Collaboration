"""SQLite persistence for one idempotent bearing review per task-bearing."""

from __future__ import annotations

import json
from hashlib import sha256
import time
import uuid
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database

from .contracts import BearingReviewConflictError, EXPECTED_PACKET_COUNT


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class BearingReviewRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def create_or_get(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM bearing_review WHERE device_id=? AND task_id=? AND bearing_id=?",
                (request["device_id"], request["task_id"], request["bearing_id"]),
            ).fetchone()
            if existing:
                stored = dict(existing)
                if stored["sender_id"] != request["sender_id"] or stored["packet_manifest_sha256"] != request["packet_manifest_sha256"]:
                    raise BearingReviewConflictError("BEARING_REVIEW_MANIFEST_CONFLICT")
                return stored, False
            bearing_review_id = str(uuid.uuid4())
            raw_context_request_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO bearing_review(bearing_review_id,device_id,task_id,bearing_id,sender_id,edge_state,edge_confidence,packet_count,packet_manifest_sha256,packet_manifest_json,status,raw_context_request_id,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bearing_review_id, request["device_id"], request["task_id"], request["bearing_id"], request["sender_id"], request["edge_state"], request["edge_confidence"], EXPECTED_PACKET_COUNT, request["packet_manifest_sha256"], _json(request["source_packet_manifest"]), "WAITING_FOR_CONTEXT", raw_context_request_id, now, now),
            )
            connection.execute(
                "INSERT INTO bearing_raw_context_request(request_id,bearing_review_id,device_id,task_id,bearing_id,sender_id,expected_packet_count,requested_packets_json,request_status,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (raw_context_request_id, bearing_review_id, request["device_id"], request["task_id"], request["bearing_id"], request["sender_id"], EXPECTED_PACKET_COUNT, _json(request["source_packet_manifest"]), "created", now, now),
            )
            return self._get_in_connection(connection, bearing_review_id), True

    def mark_dispatched(self, bearing_review_id: str) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("UPDATE bearing_raw_context_request SET request_status='dispatched', last_error_code=NULL, updated_at_ns=? WHERE bearing_review_id=?", (now, bearing_review_id))

    def mark_dispatch_failed(self, bearing_review_id: str, error_code: str) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("UPDATE bearing_raw_context_request SET request_status='dispatch_failed', last_error_code=?, updated_at_ns=? WHERE bearing_review_id=?", (error_code, now, bearing_review_id))

    def get(self, bearing_review_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM bearing_review WHERE bearing_review_id=?", (bearing_review_id,)).fetchone()
        return dict(row) if row else None

    def context_request(self, request_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT r.*,q.request_status,q.requested_packets_json FROM bearing_review r "
                "JOIN bearing_raw_context_request q ON q.bearing_review_id=r.bearing_review_id "
                "WHERE q.request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_context_packet(self, bearing_review_id: str, packet: dict[str, Any]) -> str:
        serialized = _json(packet)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_sha256 FROM bearing_review_context_packet WHERE bearing_review_id=? AND packet_id=?",
                (bearing_review_id, packet["packet_id"]),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != digest:
                    raise BearingReviewConflictError("CONTEXT_PACKET_CONFLICT")
                return "duplicate"
            occupied = connection.execute(
                "SELECT packet_id FROM bearing_review_context_packet WHERE bearing_review_id=? AND sequence_number=?",
                (bearing_review_id, packet["sequence_number"]),
            ).fetchone()
            if occupied:
                raise BearingReviewConflictError("CONTEXT_PACKET_CONFLICT")
            connection.execute(
                "INSERT INTO bearing_review_context_packet(bearing_review_id,packet_id,sequence_number,payload_sha256,packet_json,received_at_ns) VALUES (?,?,?,?,?,?)",
                (bearing_review_id, packet["packet_id"], packet["sequence_number"], digest, serialized, now),
            )
        return "accepted"

    def mark_processing_if_complete(self, bearing_review_id: str) -> int:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM bearing_review_context_packet WHERE bearing_review_id=?",
                (bearing_review_id,),
            ).fetchone()["count"]
            if count == EXPECTED_PACKET_COUNT:
                connection.execute(
                    "UPDATE bearing_review SET status='PROCESSING', error_code=NULL, updated_at_ns=? WHERE bearing_review_id=? AND status='WAITING_FOR_CONTEXT'",
                    (now, bearing_review_id),
                )
                connection.execute(
                    "UPDATE bearing_raw_context_request SET request_status='complete', last_error_code=NULL, updated_at_ns=? WHERE bearing_review_id=?",
                    (now, bearing_review_id),
                )
        return count

    def progress(self, bearing_review_id: str) -> tuple[int, int]:
        with connect(self.database_path) as connection:
            received = connection.execute(
                "SELECT COUNT(*) AS count FROM bearing_review_context_packet WHERE bearing_review_id=?",
                (bearing_review_id,),
            ).fetchone()["count"]
        return received, EXPECTED_PACKET_COUNT

    def context_packets(self, bearing_review_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT packet_json FROM bearing_review_context_packet WHERE bearing_review_id=? ORDER BY sequence_number",
                (bearing_review_id,),
            ).fetchall()
        return [json.loads(row["packet_json"]) for row in rows]

    def complete(self, bearing_review_id: str, *, aggregation: dict[str, Any], result: dict[str, Any]) -> None:
        now = time.time_ns()
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO bearing_review_aggregation(aggregation_id,bearing_review_id,packet_count,packet_manifest_json,sample_rate_hz,total_sample_count,aggregation_status,enhanced_features_json,created_at_ns) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(bearing_review_id) DO NOTHING",
                (str(uuid.uuid4()), bearing_review_id, EXPECTED_PACKET_COUNT, _json(aggregation["packet_manifest"]), aggregation["sample_rate_hz"], aggregation["total_sample_count"], "succeeded", _json(aggregation["enhanced_features"]), now),
            )
            connection.execute(
                "UPDATE bearing_review SET status='SUCCEEDED', result_json=?, error_code=NULL, updated_at_ns=? WHERE bearing_review_id=?",
                (_json(result), now, bearing_review_id),
            )

    @staticmethod
    def _get_in_connection(connection: Any, bearing_review_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM bearing_review WHERE bearing_review_id=?", (bearing_review_id,)).fetchone()
        return dict(row)
