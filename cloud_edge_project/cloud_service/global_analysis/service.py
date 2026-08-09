"""云端全局分析流程编排。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cloud_service.global_analysis.common_analyzer import (
    DEFAULT_TASK_LIMIT,
    analyze_arbitration_success,
    analyze_conflict_rate,
    analyze_edge_cloud_agreement,
    analyze_state_trend,
)
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from core.scenario_errors import UnsupportedScenarioError
from scenarios.bearing.cloud.global_analysis.analyzer import (
    analyze_bearing_risk,
    build_bearing_maintenance_recommendations,
)
from scenarios.bearing.cloud.global_analysis.data_loader import load_bearing_scenario_data


class GlobalAnalysisService:
    """协调公共统计、场景分析和最终结果保存。"""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.repository = GlobalAnalysisResultRepository(self.database_path)

    def analyze(
        self,
        scenario_type: str,
        subject_id: str,
        task_limit: int = DEFAULT_TASK_LIMIT,
    ) -> dict[str, Any]:
        """执行一次指定场景和分析对象的全局分析。"""

        normalized_scenario = _required_identifier(scenario_type, "scenario_type")
        normalized_subject = _required_identifier(subject_id, "subject_id")
        if normalized_scenario != "bearing":
            raise UnsupportedScenarioError(normalized_scenario)
        if isinstance(task_limit, bool) or not isinstance(task_limit, int) or task_limit < 1:
            raise ValueError("task_limit 必须是正整数")

        data = load_bearing_scenario_data(
            self.database_path, normalized_subject, task_limit
        )
        state_trend = analyze_state_trend(data["device_tasks"])
        missing_tables = data["missing_upstream_tables"]
        if missing_tables:
            state_trend = {
                **state_trend,
                "trend": "insufficient_data",
                "trend_delta": None,
                "missing_upstream_tables": missing_tables,
            }
        bearing_risk = analyze_bearing_risk(data["bearing_tasks"])
        edge_model = analyze_edge_cloud_agreement(data["edge_cloud_pairs"])
        arbitration = {
            **analyze_conflict_rate(data["device_tasks"]),
            **analyze_arbitration_success(data["arbitrations"]),
        }
        result = {
            "schema_version": "global_analysis_result/1.0",
            "analysis_id": f"ga_{uuid4().hex}",
            "status": (
                "insufficient_data"
                if state_trend["trend"] == "insufficient_data"
                else "succeeded"
            ),
            "scenario_type": normalized_scenario,
            "subject_id": normalized_subject,
            "analysis_window": {
                "task_limit": task_limit,
                "actual_task_count": len(data["device_tasks"]),
            },
            "common_analysis": {
                "state_trend": state_trend,
                "edge_model": edge_model,
                "arbitration": arbitration,
            },
            "scenario_result": {"bearing_risk": bearing_risk},
            "recommendations": {
                "maintenance": build_bearing_maintenance_recommendations(
                    state_trend, bearing_risk
                ),
                "model": _build_model_recommendations(edge_model),
            },
            "summary": None,
            "created_at_ns": time.time_ns(),
        }
        self.repository.save_result(result)
        return result


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


def _build_model_recommendations(edge_model: dict[str, Any]) -> list[str]:
    if edge_model["reviewed_packet_count"] < 10:
        return ["当前云端复核样本较少，建议继续积累数据。"]
    if edge_model["cloud_correction_rate"] >= 0.15:
        return ["边缘模型在云端复核样本中的修正比例较高，建议进一步评估模型更新。"]
    return []
