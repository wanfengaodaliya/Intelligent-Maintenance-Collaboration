from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect
from cloud_service.device_arbitration.errors import ArbitrationPayloadConflictError


class DeviceArbitrationRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def get_by_conflict_id(self, conflict_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT result_json FROM device_arbitration_record "
                "WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def get_request_payload_hash(self, conflict_id: str) -> str | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT request_payload_hash FROM device_arbitration_record WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
        if row is None:
            return None
        return row["request_payload_hash"]

    def list_recent(
        self, device_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            if device_id is None:
                rows = connection.execute(
                    """SELECT result_json FROM device_arbitration_record
                       WHERE result_json IS NOT NULL
                       ORDER BY created_at_ns DESC, arbitration_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT result_json FROM device_arbitration_record
                       WHERE subject_id=? AND result_json IS NOT NULL
                       ORDER BY created_at_ns DESC, arbitration_id DESC LIMIT ?""",
                    (device_id, limit),
                ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def save(
        self, *, request: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO device_arbitration_record (
                    arbitration_id, conflict_id, scenario_type, subject_id, task_id,
                    status, final_action, confidence, request_json, result_json,
                    error_code, summary_result_id, window_start_sequence,
                    window_end_sequence, request_payload_hash, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(conflict_id) DO NOTHING
                """,
                (
                    result["arbitration_id"],
                    result["conflict_id"],
                    result["scenario_type"],
                    result["subject_id"],
                    result["task_id"],
                    result["status"],
                    result["final_action"],
                    result["confidence"],
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    request.get("summary_identity", {}).get("summary_result_id"),
                    request.get("summary_identity", {}).get("window_start_sequence"),
                    request.get("summary_identity", {}).get("window_end_sequence"),
                    request.get("request_payload_hash"),
                    result["created_at_ns"],
                ),
            )
            row = connection.execute(
                "SELECT result_json, request_payload_hash FROM device_arbitration_record "
                "WHERE conflict_id=?",
                (result["conflict_id"],),
            ).fetchone()
        if row is None or row["result_json"] is None:
            raise RuntimeError("device arbitration result was not persisted")
        incoming_hash = request.get("request_payload_hash")
        if (
            row["request_payload_hash"] is not None
            and isinstance(incoming_hash, str)
            and row["request_payload_hash"] != incoming_hash
        ):
            raise ArbitrationPayloadConflictError(
                "conflict_id already belongs to a different arbitration payload"
            )
        return json.loads(row["result_json"])
