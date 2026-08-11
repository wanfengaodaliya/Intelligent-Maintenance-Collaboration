"""SQLite adapter for normalized bearing global-analysis inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect


class SQLiteGlobalAnalysisDataSource:
    """Read existing upstream records only; never synthesise review outcomes."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def load(self, device_id: str, task_limit: int) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            availability = {
                "device_tasks": _table_exists(connection, "device_task_result"),
                "bearing_tasks": _table_exists(connection, "bearing_task_result"),
                "bearing_review_pairs": _table_exists(connection, "bearing_task_result"),
                "arbitrations": _table_exists(connection, "device_arbitration_record"),
                "packet_review_pairs": _table_exists(connection, "packet_review_pair"),
            }
            device_tasks = _load_device_tasks(connection, device_id, task_limit) if availability["device_tasks"] else []
            task_ids = [row["task_id"] for row in device_tasks]
            bearing_tasks = _load_bearing_tasks(connection, device_id, task_ids) if availability["bearing_tasks"] else []
            packet_pairs = _load_packet_pairs(connection, device_id, task_ids) if availability["packet_review_pairs"] else []
            arbitrations = _load_arbitrations(connection, device_id, task_ids) if availability["arbitrations"] else []
        return {
            "device_tasks": device_tasks,
            "bearing_tasks": bearing_tasks,
            "packet_review_pairs": packet_pairs,
            "bearing_review_pairs": [row for row in bearing_tasks if row.get("cloud_reviewed")],
            "arbitrations": arbitrations,
            "availability": availability,
        }


def load_bearing_scenario_data(database_path: Path, device_id: str, task_limit: int) -> dict[str, Any]:
    """Backward-compatible entry point for callers using the former loader."""
    return SQLiteGlobalAnalysisDataSource(database_path).load(device_id, task_limit)


def _load_device_tasks(connection: Any, device_id: str, task_limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT device_id,task_id,final_state,confidence,has_conflict,arbitration_id,completed_at_ns
           FROM device_task_result WHERE device_id=? ORDER BY completed_at_ns DESC LIMIT ?""",
        (device_id, task_limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _load_bearing_tasks(connection: Any, device_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"""SELECT device_id,task_id,bearing_id,bearing_state,edge_state,edge_confidence,
                    cloud_reviewed,cloud_state,cloud_confidence,result_source,model_version,
                    completed_at_ns,result_json
             FROM bearing_task_result WHERE device_id=? AND task_id IN ({placeholders})
             ORDER BY completed_at_ns,bearing_id""",
        (device_id, *task_ids),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        upstream = _json_object(item.pop("result_json", None))
        item["cloud_reviewed"] = bool(item["cloud_reviewed"])
        item["aggregation_version"] = upstream.get("aggregation_version") or item.get("model_version")
        item["cloud_bearing_review_version"] = upstream.get("cloud_bearing_review_version")
        item["review_trigger_reason"] = upstream.get("review_trigger_reason")
        result.append(item)
    return result


def _load_packet_pairs(connection: Any, device_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"""SELECT device_id,task_id,bearing_id,packet_id,edge_label,edge_confidence,
                    edge_model_version,cloud_label,cloud_confidence,cloud_model_version,
                    operating_context_json,created_at_ns
             FROM packet_review_pair WHERE device_id=? AND task_id IN ({placeholders})
             ORDER BY created_at_ns""",
        (device_id, *task_ids),
    ).fetchall()
    return [{**dict(row), "operating_context": _json_object(row["operating_context_json"])} for row in rows]


def _load_arbitrations(connection: Any, device_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"""SELECT arbitration_id,subject_id AS device_id,task_id,status,final_action,
                    result_json,created_at_ns FROM device_arbitration_record
             WHERE scenario_type='bearing' AND subject_id=? AND task_id IN ({placeholders})
             ORDER BY created_at_ns""",
        (device_id, *task_ids),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.update({key: value for key, value in _json_object(item.pop("result_json", None)).items() if key in {"resolution_method", "dominant_bearing_id", "rule_version"}})
        result.append(item)
    return result


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _table_exists(connection: Any, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None
