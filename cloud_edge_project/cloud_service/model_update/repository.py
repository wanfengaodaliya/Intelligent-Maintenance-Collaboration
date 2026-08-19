"""SQLite persistence for cloud model-update tasks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


JSON_FIELDS = {
    "problem_context_json": "problem_context",
    "evidence_snapshot_json": "evidence_snapshot",
    "trainer_plan_json": "trainer_plan",
    "candidate_artifact_json": "candidate_artifact",
    "validation_result_json": "validation_result",
    "confirmation_result_json": "confirmation_result",
    "distribution_result_json": "distribution_result",
    "post_validation_result_json": "post_validation_result",
    "rollback_result_json": "rollback_result",
}


class ModelUpdateRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM global_analysis_result WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def find_by_analysis_problem(
        self, analysis_id: str, problem_id: str
    ) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM model_update_task WHERE analysis_id=? AND problem_id=?",
                (analysis_id, problem_id),
            ).fetchone()
        return _decode(dict(row)) if row else None

    def create(self, task: dict[str, Any]) -> dict[str, Any]:
        columns = tuple(task)
        values = tuple(_encode_value(key, task[key]) for key in columns)
        with connect(self.database_path) as connection:
            connection.execute(
                f"INSERT INTO model_update_task ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                values,
            )
        return self.get(task["update_id"])

    def get(self, update_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM model_update_task WHERE update_id=?", (update_id,)
            ).fetchone()
        return _decode(dict(row)) if row else None

    def update(self, update_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            return self.get(update_id)
        encoded = {key: _encode_value(key, value) for key, value in changes.items()}
        assignments = ", ".join(f"{key}=?" for key in encoded)
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                f"UPDATE model_update_task SET {assignments} WHERE update_id=?",
                (*encoded.values(), update_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(update_id)
        return self.get(update_id)


def _encode_value(key: str, value: Any) -> Any:
    if key in JSON_FIELDS and value is not None:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return value


def _decode(task: dict[str, Any]) -> dict[str, Any]:
    for source, target in JSON_FIELDS.items():
        value = task.pop(source)
        task[target] = json.loads(value) if value else None
    task["rollback_requested"] = bool(task["rollback_requested"])
    return task
