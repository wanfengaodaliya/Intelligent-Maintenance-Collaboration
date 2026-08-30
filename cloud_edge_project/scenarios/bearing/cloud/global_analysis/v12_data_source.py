"""Read-only V1.2 inputs for GlobalAnalysis.

The edge owns revisions.  Cloud therefore derives the current view by selecting
the highest immutable revision for each bearing or device decision identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect


class V12GlobalAnalysisDataSource:
    """Load V1.2 current decisions and their independent physical evidence."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def load(self, device_id: str, task_limit: int) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            availability = {
                "v12_bearing_results": _table_exists(connection, "cloud_bearing_diagnosis_result"),
                "v12_device_results": _table_exists(connection, "cloud_device_decision_result"),
                "physical_evidence": _table_exists(connection, "physical_evidence_result")
                and _table_exists(connection, "raw_analysis_sample"),
                "edge_summaries": _table_exists(connection, "edge_packet_summary"),
                "packet_review_pairs": _table_exists(connection, "cloud_moment_review_record"),
                "summary_windows": _table_exists(connection, "summary_window_record"),
            }
            bearing_records = _load_records(connection, "cloud_bearing_diagnosis_result", device_id) if availability["v12_bearing_results"] else []
            device_records = _load_records(connection, "cloud_device_decision_result", device_id) if availability["v12_device_results"] else []
            current_devices = _current(device_records, _device_key)
            current_devices.sort(key=lambda row: (row.get("closed_at_ns", 0), row["task_id"], row["decision_round_id"]))
            current_devices = current_devices[-task_limit:]
            selected_rounds = {(row["task_id"], row["decision_round_id"]) for row in current_devices}
            scoped_bearing_records = [
                row for row in bearing_records
                if (row["task_id"], row["decision_round_id"]) in selected_rounds
            ]
            current_bearings = _current(scoped_bearing_records, _bearing_key)
            current_bearings.sort(key=lambda row: (row.get("created_at_ns", 0), row["bearing_id"]))
            evidence = _load_evidence(connection, device_id, selected_rounds) if availability["physical_evidence"] else []
            edge_summaries = _load_edge_summaries(connection, device_id, selected_rounds) if availability["edge_summaries"] else []
            arbitration_ids = [str(row["arbitration_id"]) for row in current_devices if row.get("arbitration_id")]
            arbitration_states = _load_arbitration_states(connection, arbitration_ids) if arbitration_ids else {}
            packet_review_pairs = _load_packet_review_pairs(
                connection, device_id, selected_rounds
            ) if availability["packet_review_pairs"] else []
            summary_windows = (
                _load_summary_windows(connection, device_id, task_limit)
                if availability["summary_windows"]
                else []
            )
            summary_arbitrations = _load_summary_arbitrations(
                connection,
                device_id,
                {row["summary_result_id"] for row in summary_windows},
            )

        return {
            "device_tasks": current_devices,
            "bearing_tasks": current_bearings,
            "bearing_review_pairs": _bearing_review_pairs(scoped_bearing_records),
            "packet_review_pairs": packet_review_pairs,
            "summary_windows": summary_windows,
            "arbitrations": summary_arbitrations,
            "physical_evidence": evidence,
            "edge_summaries": edge_summaries,
            "revision_deduplication": {
                "bearing_revision_record_count": len(scoped_bearing_records),
                "current_bearing_result_count": len(current_bearings),
                "superseded_bearing_revision_count": len(scoped_bearing_records) - len(current_bearings),
                "device_revision_record_count": len(device_records),
                "current_device_result_count": len(current_devices),
                "superseded_device_revision_count": len(device_records) - len(_current(device_records, _device_key)),
            },
            "round_closure_analysis": _round_closure_analysis(current_devices, scoped_bearing_records),
            "availability": availability,
        }


def _load_records(connection: Any, table: str, device_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT payload_json,received_at_ns FROM {table} WHERE device_id=? ORDER BY received_at_ns,result_id",
        (device_id,),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        if payload:
            payload["received_at_ns"] = row["received_at_ns"]
            records.append(payload)
    return records


def _current(records: list[dict[str, Any]], key) -> list[dict[str, Any]]:
    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in records:
        identity = key(row)
        prior = selected.get(identity)
        rank = (int(row.get("revision", 0)), int(row.get("received_at_ns", 0)), str(row.get("result_id", "")))
        if prior is None or rank > (int(prior.get("revision", 0)), int(prior.get("received_at_ns", 0)), str(prior.get("result_id", ""))):
            selected[identity] = row
    return list(selected.values())


def _device_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["device_id"], row["task_id"], row["decision_round_id"]


def _bearing_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return row["device_id"], row["task_id"], row["decision_round_id"], row["bearing_id"], row["diagnosis_window_id"]


def _bearing_review_pairs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in records:
        grouped.setdefault(_bearing_key(row), []).append(row)
    pairs: list[dict[str, Any]] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: (int(row.get("revision", 0)), int(row.get("received_at_ns", 0))))
        current = rows[-1]
        if current.get("review_status") != "REVIEWED" or not current.get("cloud_result_id"):
            continue
        edge = rows[0]
        pairs.append({
            "device_id": current["device_id"], "task_id": current["task_id"],
            "bearing_id": current["bearing_id"], "edge_state": edge.get("bearing_state"),
            "cloud_state": current.get("bearing_state"), "cloud_reviewed": True,
            "aggregation_version": current.get("model_version"),
            "completed_at_ns": current.get("created_at_ns", 0),
        })
    return pairs


def _round_closure_analysis(devices: list[dict[str, Any]], bearing_records: list[dict[str, Any]]) -> dict[str, int]:
    timeout_rounds = {
        (row["task_id"], row["decision_round_id"])
        for row in devices if row.get("closure_reason") == "ROUND_TIMEOUT"
    }
    return {
        "round_timeout_count": len(timeout_rounds),
        "late_bearing_revision_count": sum(
            row.get("lifecycle_state") == "LATE_CLOUD_CORRECTED"
            and (row["task_id"], row["decision_round_id"]) in timeout_rounds
            for row in bearing_records
        ),
    }


def _load_evidence(connection: Any, device_id: str, selected_rounds: set[tuple[str, str]]) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT sample.metadata_json,evidence.result_json,evidence.limitations_json,evidence.status
           FROM raw_analysis_sample AS sample JOIN physical_evidence_result AS evidence
             ON evidence.sample_id=sample.sample_id
           WHERE json_extract(sample.metadata_json, '$.device_id')=? AND evidence.status='SUCCEEDED'
           ORDER BY sample.created_at_ns,sample.sample_id""",
        (device_id,),
    ).fetchall()
    values = []
    for row in rows:
        metadata = _json_object(row["metadata_json"])
        if (metadata.get("task_id"), metadata.get("decision_round_id")) not in selected_rounds:
            continue
        values.append({
            **metadata, "result": _json_object(row["result_json"]),
            "limitations": _json_list(row["limitations_json"]),
        })
    return values


def _load_edge_summaries(connection: Any, device_id: str, selected_rounds: set[tuple[str, str]]) -> list[dict[str, Any]]:
    if not selected_rounds:
        return []
    task_ids = sorted({task_id for task_id, _ in selected_rounds})
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"SELECT task_id,sequence_number FROM edge_packet_summary WHERE device_id=? AND task_id IN ({placeholders})",
        (device_id, *task_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_packet_review_pairs(
    connection: Any, device_id: str, selected_rounds: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    if not selected_rounds:
        return []
    task_ids = sorted({task_id for task_id, _ in selected_rounds})
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        f"SELECT device_id,task_id,bearing_id,decision_round_id,diagnosis_window_id,"
        f"bearing_state,edge_label,model_version,created_at_ns "
        f"FROM cloud_moment_review_record "
        f"WHERE device_id=? AND task_id IN ({placeholders}) "
        f"AND edge_label IS NOT NULL ORDER BY created_at_ns,review_id",
        (device_id, *task_ids),
    ).fetchall()
    return [
        {
            "device_id": row["device_id"],
            "task_id": row["task_id"],
            "bearing_id": row["bearing_id"],
            "bearing_decision_round_id": row["decision_round_id"],
            "diagnosis_window_id": row["diagnosis_window_id"],
            "edge_label": row["edge_label"],
            "cloud_label": row["bearing_state"],
            "edge_model_version": row["model_version"],
            "completed_at_ns": row["created_at_ns"],
        }
        for row in rows
    ]


def _load_arbitration_states(
    connection: Any, arbitration_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Read real arbitration status from device_arbitration_record (not hardcoded)."""
    if not arbitration_ids:
        return {}
    placeholders = ",".join("?" for _ in arbitration_ids)
    rows = connection.execute(
        f"SELECT arbitration_id,status,final_action,confidence FROM device_arbitration_record "
        f"WHERE arbitration_id IN ({placeholders})",
        arbitration_ids,
    ).fetchall()
    return {str(row["arbitration_id"]): dict(row) for row in rows}


def _load_summary_windows(
    connection: Any, device_id: str, limit: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT payload_json FROM summary_window_record
        WHERE device_id=? ORDER BY created_at_ns DESC, summary_result_id DESC LIMIT ?
        """,
        (device_id, int(limit)),
    ).fetchall()
    values = [_json_object(row["payload_json"]) for row in rows]
    return [value for value in values if value]


def _load_summary_arbitrations(
    connection: Any, device_id: str, summary_result_ids: set[str]
) -> list[dict[str, Any]]:
    if not summary_result_ids:
        return []
    placeholders = ",".join("?" for _ in summary_result_ids)
    rows = connection.execute(
        f"""
        SELECT result_json FROM device_arbitration_record
        WHERE subject_id=? AND summary_result_id IN ({placeholders})
        AND result_json IS NOT NULL ORDER BY created_at_ns, arbitration_id
        """,
        (device_id, *sorted(summary_result_ids)),
    ).fetchall()
    values = [_json_object(row["result_json"]) for row in rows]
    return [value for value in values if value]


def _table_exists(connection: Any, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _json_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        decoded = None
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: object) -> list[Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        decoded = None
    return decoded if isinstance(decoded, list) else []
