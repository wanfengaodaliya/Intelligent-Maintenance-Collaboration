"""Orchestration for read-only historical global analysis."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import DEFAULT_TASK_LIMIT, GlobalAnalysisConfig
from cloud_service.global_analysis.device_health_analyzer import analyze_device_health
from cloud_service.global_analysis.packet_model_analyzer import analyze_packet_model
from cloud_service.global_analysis.problem_detector import detect_problem_candidates
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from core.scenario_errors import UnsupportedScenarioError
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import analyze_bearing_aggregation
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import analyze_bearing_risk
from scenarios.bearing.cloud.global_analysis.config import DEFAULT_GLOBAL_ANALYSIS_CONFIG
from scenarios.bearing.cloud.global_analysis.data_loader import SQLiteGlobalAnalysisDataSource
from scenarios.bearing.cloud.global_analysis.data_source import GlobalAnalysisDataSource


class GlobalAnalysisService:
    """Coordinates data loading, pure analysis, candidate detection and storage."""

    def __init__(
        self,
        database_path: Path,
        data_source: GlobalAnalysisDataSource | None = None,
        config: GlobalAnalysisConfig = DEFAULT_GLOBAL_ANALYSIS_CONFIG,
    ) -> None:
        self.database_path = Path(database_path)
        self.repository = GlobalAnalysisResultRepository(self.database_path)
        self.data_source = data_source or SQLiteGlobalAnalysisDataSource(self.database_path)
        self.config = config

    def analyze(self, scenario_type: str, subject_id: str, task_limit: int = DEFAULT_TASK_LIMIT) -> dict[str, Any]:
        scenario = _required_identifier(scenario_type, "scenario_type")
        subject = _required_identifier(subject_id, "subject_id")
        if scenario != "bearing":
            raise UnsupportedScenarioError(scenario)
        if isinstance(task_limit, bool) or not isinstance(task_limit, int) or task_limit < 1:
            raise ValueError("task_limit 必须是正整数")
        data = self.data_source.load(subject, task_limit)
        availability = data.get("availability", {})
        device_health = analyze_device_health(data["device_tasks"], self.config)
        bearing_risk = analyze_bearing_risk(data["bearing_tasks"], self.config)
        packet_diagnosis = analyze_packet_model(
            data["packet_review_pairs"], self.config,
            available=availability.get("packet_review_pairs", True),
        )
        bearing_aggregation = analyze_bearing_aggregation(data["bearing_review_pairs"], self.config)
        device_arbitration = analyze_device_arbitration(data["device_tasks"], data["arbitrations"], self.config)
        previous = self.repository.get_recent(scenario, subject, 3)
        candidates = detect_problem_candidates(
            device_health=device_health, bearing_risk=bearing_risk,
            packet_diagnosis=packet_diagnosis, bearing_aggregation=bearing_aggregation,
            device_arbitration=device_arbitration, previous_analysis=previous, config=self.config,
        )
        result = {
            "schema_version": "global_analysis_result/2.0",
            "analysis_id": f"ga_{uuid4().hex}",
            "status": "succeeded" if device_health["status"] == "succeeded" else "insufficient_data",
            "scenario_type": scenario,
            "subject_id": subject,
            "analysis_window": {"task_limit": task_limit, "actual_task_count": len(data["device_tasks"])},
            "device_health_analysis": device_health,
            "bearing_risk_analysis": bearing_risk,
            "packet_diagnosis_analysis": packet_diagnosis,
            "bearing_aggregation_analysis": bearing_aggregation,
            "device_arbitration_analysis": device_arbitration,
            "problem_candidates": candidates,
            "maintenance_recommendations": _maintenance_recommendations(device_health, bearing_risk),
            "created_at_ns": time.time_ns(),
        }
        self.repository.save_result(result)
        return result


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


def _maintenance_recommendations(device_health: dict[str, Any], bearing_risk: dict[str, Any]) -> list[str]:
    recommendations = []
    if device_health.get("trend") == "degrading":
        recommendations.append("设备近期健康状态呈恶化趋势，建议重点关注。")
    primary = bearing_risk.get("primary_risk_bearing_id")
    if primary:
        recommendations.append(f"建议优先检查风险最高的轴承 {primary}。")
    return recommendations
