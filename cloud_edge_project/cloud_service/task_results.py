"""Idempotent cloud persistence for upstream bearing and device task results."""
from __future__ import annotations
import json, time
from dataclasses import fields
from pathlib import Path
from typing import Any
from cloud_service.storage.database import connect, initialize_database
from core.diagnosis_contracts import BearingDecisionResult, DeviceDecisionResult

class TaskResultService:
    def __init__(self, database_path: Path):
        self.database_path=Path(database_path); initialize_database(self.database_path)
        self._initialize_v12()
    def ingest_bearing(self, payload: dict[str, Any]) -> dict[str, str]:
        self._require(payload, ("device_id","task_id","bearing_id","edge_state","edge_confidence","bearing_state","result_source","packet_count","source_packet_manifest"))
        now=time.time_ns(); raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        with connect(self.database_path) as c:
            c.execute("INSERT INTO bearing_task_result(device_id,task_id,bearing_id,edge_state,edge_confidence,cloud_reviewed,cloud_state,cloud_confidence,bearing_state,result_source,packet_count,source_packet_manifest,model_version,completed_at_ns,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,task_id,bearing_id) DO NOTHING",(payload['device_id'],payload['task_id'],payload['bearing_id'],payload['edge_state'],payload['edge_confidence'],int(payload.get('cloud_reviewed',False)),payload.get('cloud_state'),payload.get('cloud_confidence'),payload['bearing_state'],payload['result_source'],payload['packet_count'],json.dumps(payload['source_packet_manifest'],ensure_ascii=False),payload.get('model_version'),payload.get('completed_at_ns',now),raw))
        return {"status":"accepted"}
    def ingest_device(self, payload: dict[str, Any]) -> dict[str, str]:
        self._require(payload,("device_id","task_id","final_state","confidence","has_conflict")); now=time.time_ns(); raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        with connect(self.database_path) as c:
            c.execute("INSERT INTO device_task_result(device_id,task_id,final_state,confidence,has_conflict,arbitration_id,summary,completed_at_ns,result_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,task_id) DO NOTHING",(payload['device_id'],payload['task_id'],payload['final_state'],payload['confidence'],int(payload['has_conflict']),payload.get('arbitration_id'),payload.get('summary'),payload.get('completed_at_ns',now),raw))
        return {"status":"accepted"}

    def ingest_bearing_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._ingest_v12("cloud_bearing_diagnosis_result", payload, BearingDecisionResult)

    def ingest_device_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._ingest_v12("cloud_device_decision_result", payload, DeviceDecisionResult)

    def get_device_decision(self, result_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM cloud_device_decision_result WHERE result_id=?",
                (result_id,),
            ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def list_recent_device_decisions(
        self, device_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            if device_id is None:
                rows = connection.execute(
                    """SELECT payload_json FROM cloud_device_decision_result
                       ORDER BY COALESCE(
                           json_extract(payload_json, '$.closed_at_ns'),
                           json_extract(payload_json, '$.created_at_ns'),
                           received_at_ns
                       ) DESC, result_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT payload_json FROM cloud_device_decision_result
                       WHERE device_id=?
                       ORDER BY COALESCE(
                           json_extract(payload_json, '$.closed_at_ns'),
                           json_extract(payload_json, '$.created_at_ns'),
                           received_at_ns
                       ) DESC, result_id DESC LIMIT ?""",
                    (device_id, limit),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _ingest_v12(self, table: str, payload: dict[str, Any], contract: Any) -> dict[str, Any]:
        required = {field.name for field in fields(contract)}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("INVALID_V12_RESULT")
        result_id = payload.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ValueError("INVALID_V12_RESULT")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT payload_json FROM {table} WHERE result_id=?", (result_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != raw:
                    raise ValueError("RESULT_ID_CONFLICT")
                return {"status": "accepted", "duplicate": True}
            connection.execute(
                f"""INSERT INTO {table}(
                result_id,device_id,task_id,decision_round_id,revision,replaces_result_id,payload_json,received_at_ns
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    result_id, payload["device_id"], payload["task_id"], payload["decision_round_id"],
                    payload["revision"], payload["replaces_result_id"], raw, time.time_ns(),
                ),
            )
        return {"status": "accepted", "duplicate": False}

    def _initialize_v12(self) -> None:
        with connect(self.database_path) as connection:
            for table in ("cloud_bearing_diagnosis_result", "cloud_device_decision_result"):
                connection.execute(
                    f"""CREATE TABLE IF NOT EXISTS {table}(
                    result_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    decision_round_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    replaces_result_id TEXT, payload_json TEXT NOT NULL, received_at_ns INTEGER NOT NULL
                    )"""
                )
    @staticmethod
    def _require(payload: Any, fields: tuple[str,...]) -> None:
        if not isinstance(payload,dict) or any(not isinstance(payload.get(k),str) or not payload[k] for k in fields if k not in {'edge_confidence','packet_count','source_packet_manifest','has_conflict','confidence'}): raise ValueError('INVALID_TASK_RESULT')
