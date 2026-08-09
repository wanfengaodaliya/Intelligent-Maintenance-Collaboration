"""SQLite persistence for cloud model-update tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


class ModelUpdateRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT analysis_id,scenario_type,subject_id,reviewed_packet_count,
                          cloud_correction_rate,result_json
                   FROM global_analysis_result WHERE analysis_id=?""",
                (analysis_id,),
            ).fetchone()
        return dict(row) if row else None

    def create(self, task: dict[str, Any]) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO model_update_task(
                    update_id,analysis_id,scenario_type,subject_id,update_type,update_reason,
                    old_version,new_version,update_file,update_file_sha256,target_edge_nodes_json,
                    test_data_limit,status,created_at_ns,updated_at_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task["update_id"], task["analysis_id"], task["scenario_type"], task["subject_id"],
                    task["update_type"], task["update_reason"], task["old_version"], task["new_version"],
                    task["update_file"], task["update_file_sha256"],
                    json.dumps(task["target_edge_nodes"], ensure_ascii=False), task["test_data_limit"],
                    task["status"], task["created_at_ns"], task["updated_at_ns"],
                ),
            )
        return self.get(task["update_id"])

    def get(self, update_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM model_update_task WHERE update_id=?", (update_id,)
            ).fetchone()
        return _decode(dict(row)) if row else None

    def update(self, update_id: str, **changes: Any) -> dict[str, Any]:
        encoded = {
            key: json.dumps(value, ensure_ascii=False) if key.endswith("_json") and value is not None else value
            for key, value in changes.items()
        }
        assignments = ", ".join(f"{key}=?" for key in encoded)
        with connect(self.database_path) as connection:
            connection.execute(
                f"UPDATE model_update_task SET {assignments} WHERE update_id=?",
                (*encoded.values(), update_id),
            )
        return self.get(update_id)


def _decode(task: dict[str, Any]) -> dict[str, Any]:
    for source, target in (
        ("target_edge_nodes_json", "target_edge_nodes"),
        ("validation_result_json", "validation_result"),
        ("confirmation_json", "confirmation"),
        ("distribution_result_json", "distribution_result"),
    ):
        value = task.pop(source)
        task[target] = json.loads(value) if value else None
    return task
