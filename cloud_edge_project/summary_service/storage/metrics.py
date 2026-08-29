from __future__ import annotations

import json
import sqlite3
from typing import Any

from .schema import OUTBOX_TABLES


def increment_counter(
    connection: sqlite3.Connection, metric: str, amount: int = 1
) -> None:
    connection.execute(
        """
        INSERT INTO summary_metrics_counter(metric, value) VALUES (?, ?)
        ON CONFLICT(metric) DO UPDATE SET value = value + excluded.value
        """,
        (metric, int(amount)),
    )


def load_metrics(
    connection: sqlite3.Connection, *, device_id: str | None = None
) -> dict[str, Any]:
    where = "WHERE device_id = ?" if device_id else ""
    params: tuple[Any, ...] = (device_id,) if device_id else ()
    window = connection.execute(
        f"""
        SELECT
            COUNT(*) AS total_windows,
            SUM(CASE WHEN excluded_from_formal_metrics = 0 THEN 1 ELSE 0 END) AS eligible_windows,
            SUM(CASE WHEN has_conflict = 1 AND excluded_from_formal_metrics = 0 THEN 1 ELSE 0 END) AS conflict_windows,
            SUM(CASE WHEN excluded_from_formal_metrics = 1 THEN 1 ELSE 0 END) AS incomplete_windows,
            SUM(CASE WHEN excluded_from_formal_metrics = 1
                AND json_extract(payload_json, '$.incomplete_reason') = 'INSUFFICIENT_EDGE_DIVERSITY'
                THEN 1 ELSE 0 END) AS same_edge_windows,
            SUM(COALESCE(json_array_length(payload_json, '$.missing_bearing_ids'), 0)) AS missing_node_count,
            SUM(CASE WHEN json_array_length(payload_json, '$.missing_bearing_ids') > 0 THEN 1 ELSE 0 END) AS missing_node_windows,
            AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_level_gap') END) AS average_action_level_gap,
            MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_level_gap') END) AS maximum_action_level_gap,
            AVG(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_score_gap') END) AS average_action_score_gap,
            MAX(CASE WHEN excluded_from_formal_metrics = 0 THEN json_extract(payload_json, '$.max_action_score_gap') END) AS maximum_action_score_gap,
            AVG(json_extract(payload_json, '$.window_close_duration_ns')) AS average_window_close_ns
        FROM summary_window_result
        {where}
        """,
        params,
    ).fetchone()
    state_rows = connection.execute(
        f"""
        SELECT payload_json
        FROM summary_window_result
        WHERE excluded_from_formal_metrics = 0
        {("AND device_id = ?" if device_id else "")}
        """,
        params,
    ).fetchall()
    state_combinations = {
        "normal_normal": 0,
        "fault_fault": 0,
        "normal_fault": 0,
        "fault_normal": 0,
    }
    for row in state_rows:
        node_states = json.loads(row["payload_json"]).get("node_states", {})
        ordered_states = [node_states[key] for key in sorted(node_states)]
        if len(ordered_states) != 2:
            continue
        combination = f"{ordered_states[0]}_{ordered_states[1]}"
        if combination in state_combinations:
            state_combinations[combination] += 1
    semantics_rows = connection.execute(
        f"""
        SELECT
            COALESCE(json_extract(payload_json, '$.conflict_semantics'), 'legacy') AS semantics,
            COUNT(*) AS count
        FROM summary_window_result
        WHERE excluded_from_formal_metrics = 0
        {("AND device_id = ?" if device_id else "")}
        GROUP BY semantics
        """,
        params,
    ).fetchall()
    outbox_where = "WHERE window.device_id = ?" if device_id else ""
    outbox = connection.execute(
        f"""
        SELECT
            COUNT(*) AS upload_windows,
            SUM(CASE WHEN outbox.state = 'ACKNOWLEDGED' THEN 1 ELSE 0 END) AS acknowledged_windows,
            SUM(CASE WHEN outbox.state IN ('PENDING', 'UPLOADING', 'RETRY_WAIT') THEN 1 ELSE 0 END) AS pending_windows,
            SUM(CASE WHEN outbox.state = 'DEAD_LETTER' THEN 1 ELSE 0 END) AS dead_letter_windows
        FROM summary_arbitration_outbox AS outbox
        JOIN summary_window_result AS window
          ON window.summary_result_id = outbox.summary_result_id
        {outbox_where}
        """,
        params,
    ).fetchone()
    counters = {
        row["metric"]: int(row["value"])
        for row in connection.execute(
            "SELECT metric, value FROM summary_metrics_counter"
        ).fetchall()
    }
    outbox_backlog: dict[str, dict[str, int]] = {}
    for table in OUTBOX_TABLES:
        rows = connection.execute(
            f"SELECT state, COUNT(*) AS count FROM {table} GROUP BY state"
        ).fetchall()
        counts = {row["state"]: int(row["count"]) for row in rows}
        outbox_backlog[table] = {
            "pending": counts.get("PENDING", 0)
            + counts.get("UPLOADING", 0)
            + counts.get("RETRY_WAIT", 0),
            "acknowledged": counts.get("ACKNOWLEDGED", 0),
            "dead_letter": counts.get("DEAD_LETTER", 0),
        }
    suggestion_tasks = {
        row["state"]: int(row["count"])
        for row in connection.execute(
            "SELECT state, COUNT(*) AS count FROM summary_suggestion_task GROUP BY state"
        ).fetchall()
    }

    total = int(window["total_windows"] or 0)
    eligible = int(window["eligible_windows"] or 0)
    conflicts = int(window["conflict_windows"] or 0)
    incomplete = int(window["incomplete_windows"] or 0)
    uploads = int(outbox["upload_windows"] or 0)
    acknowledged = int(outbox["acknowledged_windows"] or 0)
    pending = int(outbox["pending_windows"] or 0)
    dead_letter = int(outbox["dead_letter_windows"] or 0)
    return {
        "total_windows": total,
        "eligible_windows": eligible,
        "conflict_windows": conflicts,
        "incomplete_windows": incomplete,
        "conflict_rate": conflicts / eligible if eligible else 0.0,
        "consistency_rate": (eligible - conflicts) / eligible if eligible else 0.0,
        "state_combinations": state_combinations,
        "same_edge_windows": int(window["same_edge_windows"] or 0),
        "missing_node_windows": int(window["missing_node_windows"] or 0),
        "missing_node_count": int(window["missing_node_count"] or 0),
        "average_window_close_ns": (
            float(window["average_window_close_ns"])
            if window["average_window_close_ns"] is not None
            else None
        ),
        "average_action_level_gap": (
            float(window["average_action_level_gap"])
            if window["average_action_level_gap"] is not None
            else None
        ),
        "maximum_action_level_gap": (
            int(window["maximum_action_level_gap"])
            if window["maximum_action_level_gap"] is not None
            else None
        ),
        "average_action_score_gap": (
            float(window["average_action_score_gap"])
            if window["average_action_score_gap"] is not None
            else None
        ),
        "maximum_action_score_gap": (
            float(window["maximum_action_score_gap"])
            if window["maximum_action_score_gap"] is not None
            else None
        ),
        "conflict_semantics_distribution": {
            str(row["semantics"]): int(row["count"]) for row in semantics_rows
        },
        "arbitration_upload_windows": uploads,
        "arbitration_acknowledged_windows": acknowledged,
        "arbitration_pending_windows": pending,
        "arbitration_dead_letter_windows": dead_letter,
        "arbitration_upload_success_rate": acknowledged / conflicts if conflicts else 0.0,
        "counters": counters,
        "outbox_backlog": outbox_backlog,
        "suggestion_tasks": {
            "pending": suggestion_tasks.get("PENDING", 0)
            + suggestion_tasks.get("RETRY_WAIT", 0),
            "completed": suggestion_tasks.get("COMPLETED", 0),
            "dead_letter": suggestion_tasks.get("DEAD_LETTER", 0),
        },
    }
