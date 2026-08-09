"""读取并标准化轴承场景全局分析所需的历史数据。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect


def load_bearing_scenario_data(
    database_path: Path, device_id: str, task_limit: int
) -> dict[str, Any]:
    """读取最近设备任务及其关联的轴承、复核和仲裁记录。"""

    with connect(database_path) as connection:
        if not _table_exists(connection, "device_task_result"):
            return _empty_data(["device_task_result", "bearing_task_result"])
        device_rows = connection.execute(
            """
            SELECT task_id, final_state AS state, has_conflict, completed_at_ns
            FROM device_task_result
            WHERE device_id=?
            ORDER BY completed_at_ns DESC
            LIMIT ?
            """,
            (device_id, task_limit),
        ).fetchall()
        device_tasks = [
            {
                "task_id": row["task_id"],
                "state": row["state"],
                "has_conflict": bool(row["has_conflict"]),
                "completed_at_ns": row["completed_at_ns"],
            }
            for row in reversed(device_rows)
        ]
        task_ids = [row["task_id"] for row in device_tasks]
        if not task_ids:
            return _empty_data()
        if not _table_exists(connection, "bearing_task_result"):
            return {
                "device_tasks": device_tasks,
                "bearing_tasks": [],
                "edge_cloud_pairs": [],
                "arbitrations": [],
                "missing_upstream_tables": ["bearing_task_result"],
            }
        placeholders = ",".join("?" for _ in task_ids)
        bearing_rows = _load_bearing_tasks(connection, device_id, task_ids, placeholders)
        pairs = _load_edge_cloud_pairs(connection, device_id, task_ids, placeholders)
        arbitrations = _load_arbitrations(connection, device_id, task_ids, placeholders)
    return {
        "device_tasks": device_tasks,
        "bearing_tasks": bearing_rows,
        "edge_cloud_pairs": pairs,
        "arbitrations": arbitrations,
        "missing_upstream_tables": [],
    }


def _load_bearing_tasks(connection, device_id: str, task_ids: list[str], placeholders: str) -> list[dict[str, Any]]:
    if not _table_exists(connection, "bearing_task_result"):
        return []
    rows = connection.execute(
        f"""
        SELECT bearing_id, bearing_state, completed_at_ns
        FROM bearing_task_result
        WHERE device_id=? AND task_id IN ({placeholders})
        ORDER BY completed_at_ns ASC, bearing_id ASC
        """,
        (device_id, *task_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_edge_cloud_pairs(connection, device_id: str, task_ids: list[str], placeholders: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT edge.edge_result AS edge_label,
               json_extract(summary.summary_json, '$.label') AS cloud_label,
               edge.edge_model_version
        FROM edge_packet_summary edge
        JOIN cloud_review review
          ON review.sender_id=edge.sender_id
         AND review.anchor_packet_id=edge.packet_id
         AND review.device_id=edge.device_id
         AND review.task_id=edge.task_id
         AND review.bearing_id=edge.bearing_id
        JOIN final_diagnosis_summary summary
          ON summary.review_id=review.review_id
         AND summary.status='succeeded'
        WHERE review.device_id=? AND review.task_id IN ({placeholders})
        ORDER BY review.updated_at_ns ASC
        """,
        (device_id, *task_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_arbitrations(connection, device_id: str, task_ids: list[str], placeholders: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT status
        FROM device_arbitration_record
        WHERE scenario_type='bearing' AND subject_id=? AND task_id IN ({placeholders})
        ORDER BY created_at_ns ASC
        """,
        (device_id, *task_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _table_exists(connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


def _empty_data(missing_upstream_tables: list[str] | None = None) -> dict[str, Any]:
    return {
        "device_tasks": [],
        "bearing_tasks": [],
        "edge_cloud_pairs": [],
        "arbitrations": [],
        "missing_upstream_tables": missing_upstream_tables or [],
    }
