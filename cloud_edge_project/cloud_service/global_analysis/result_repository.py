"""全局分析最终结果的 SQLite 持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloud_service.storage.database import connect, initialize_database


class GlobalAnalysisResultRepository:
    """仅保存和读取使用通用身份字段的最终全局分析结果。"""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def save_result(self, result: dict[str, Any]) -> None:
        """保存一份已计算完成的全局分析结果。"""

        device_health = result["device_health_analysis"]
        packet_diagnosis = result["packet_diagnosis_analysis"]
        arbitration = result["device_arbitration_analysis"]
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO global_analysis_result (
                    analysis_id, scenario_type, subject_id, task_count,
                    health_trend, normal_rate, warning_rate, abnormal_rate,
                    reviewed_packet_count, edge_cloud_agreement_rate,
                    cloud_correction_rate, conflict_rate, arbitration_success_rate,
                    result_json, created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["analysis_id"],
                    result["scenario_type"],
                    result["subject_id"],
                    result["analysis_window"]["actual_task_count"],
                    device_health["trend"],
                    device_health["normal_rate"],
                    device_health["warning_rate"],
                    device_health["abnormal_rate"],
                    packet_diagnosis["reviewed_packet_count"],
                    packet_diagnosis["edge_cloud_agreement_rate"],
                    packet_diagnosis["cloud_correction_rate"],
                    arbitration["conflict_rate"],
                    arbitration["arbitration_success_rate"],
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    result["created_at_ns"],
                ),
            )

    def get_latest(
        self, scenario_type: str, subject_id: str
    ) -> dict[str, Any] | None:
        """读取指定场景和分析对象最新保存的结果。"""

        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT result_json FROM global_analysis_result
                WHERE scenario_type=? AND subject_id=?
                ORDER BY created_at_ns DESC, analysis_id DESC
                LIMIT 1
                """,
                (scenario_type, subject_id),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def get_recent(
        self, scenario_type: str, subject_id: str, limit: int
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """SELECT result_json FROM global_analysis_result
                   WHERE scenario_type=? AND subject_id=?
                   ORDER BY created_at_ns DESC, analysis_id DESC LIMIT ?""",
                (scenario_type, subject_id, limit),
            ).fetchall()
        return [json.loads(row["result_json"]) for row in reversed(rows)]

    def list_recent(
        self, scenario_type: str, subject_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            if subject_id is None:
                rows = connection.execute(
                    """SELECT result_json FROM global_analysis_result
                       WHERE scenario_type=?
                       ORDER BY created_at_ns DESC, analysis_id DESC LIMIT ?""",
                    (scenario_type, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT result_json FROM global_analysis_result
                       WHERE scenario_type=? AND subject_id=?
                       ORDER BY created_at_ns DESC, analysis_id DESC LIMIT ?""",
                    (scenario_type, subject_id, limit),
                ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]
