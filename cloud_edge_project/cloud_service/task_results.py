"""Idempotent cloud persistence for upstream bearing and device task results."""
from __future__ import annotations
import json, time
from pathlib import Path
from typing import Any
from cloud_service.storage.database import connect, initialize_database

class TaskResultService:
    def __init__(self, database_path: Path):
        self.database_path=Path(database_path); initialize_database(self.database_path)
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
    @staticmethod
    def _require(payload: Any, fields: tuple[str,...]) -> None:
        if not isinstance(payload,dict) or any(not isinstance(payload.get(k),str) or not payload[k] for k in fields if k not in {'edge_confidence','packet_count','source_packet_manifest','has_conflict','confidence'}): raise ValueError('INVALID_TASK_RESULT')
