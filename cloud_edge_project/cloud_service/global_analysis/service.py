"""Orchestration for read-only historical global analysis.

Generic service that orchestrates data loading, analysis, and storage.
Scenario-specific analysis functions are injected from outside (e.g. app.py).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from compatibility.bearing_v12 import global_analysis_exports
from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.device_health_analyzer import analyze_device_health
from cloud_service.global_analysis.packet_model_analyzer import analyze_packet_model
from cloud_service.global_analysis.physical_evidence_analyzer import analyze_physical_evidence
from cloud_service.global_analysis.problem_detector import detect_problem_candidates
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from cloud_service.global_analysis.runtime_contracts import (
    DEFAULT_TASK_LIMIT,
    GlobalAnalysisRuntimeConfig,
)
from core.scenario_plugin import GlobalAnalysisRuntime


class GlobalAnalysisService:
    """Coordinates data loading, pure analysis, candidate detection and storage.

    Scenario-specific analysis functions (e.g. bearing risk, bearing review)
    are injected via keyword arguments for portability.  When not provided,
    these analyses are skipped and the result omits their fields.
    """

    def __init__(
        self,
        database_path: Path,
        data_source: Any | None = None,
        config: GlobalAnalysisRuntimeConfig | None = None,
        *,
        runtime: GlobalAnalysisRuntime | None = None,
        # Optional scenario-specific analysis callables.
        # Each receives (data, config) and returns a dict.
        # Defaults to None = skip that analysis step.
        scenario_analyzers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.repository = GlobalAnalysisResultRepository(self.database_path)
        selected_runtime = runtime or global_analysis_exports.build_legacy_global_analysis_runtime(
            self.database_path,
            data_source=data_source,
            config=config,
            scenario_analyzers=scenario_analyzers,
        )
        self.data_source = selected_runtime.data_source
        self.config = selected_runtime.config
        self.analyze_scenario = selected_runtime.analyze_scenario
        self.detect_scenario_candidates = selected_runtime.detect_scenario_candidates

    def analyze(self, scenario_type: str, subject_id: str, task_limit: int = DEFAULT_TASK_LIMIT) -> dict[str, Any]:
        scenario = _required_identifier(scenario_type, "scenario_type")
        subject = _required_identifier(subject_id, "subject_id")
        if isinstance(task_limit, bool) or not isinstance(task_limit, int) or task_limit < 1:
            raise ValueError("task_limit must be a positive integer")
        data = self.data_source.load(subject, task_limit)
        availability = data.get("availability", {})
        device_health = analyze_device_health(data["device_tasks"], self.config)
        packet_diagnosis = analyze_packet_model(
            data["packet_review_pairs"], self.config,
            available=availability.get("packet_review_pairs", True),
        )
        device_arbitration = analyze_device_arbitration(data["device_tasks"], data["arbitrations"], self.config)
        physical_evidence = analyze_physical_evidence(
            data.get("physical_evidence", []),
            edge_summary_count=len(data.get("edge_summaries", [])),
            available=availability.get("physical_evidence", False),
        )
        common_results = {
            "device_health_analysis": device_health,
            "packet_diagnosis_analysis": packet_diagnosis,
            "device_arbitration_analysis": device_arbitration,
            "physical_evidence_analysis": physical_evidence,
        }
        scenario_results = self.analyze_scenario(
            data,
            common_results,
            self.config,
        )
        previous = self.repository.get_recent(scenario, subject, 3)
        candidates = detect_problem_candidates(
            device_health=device_health,
            packet_diagnosis=packet_diagnosis,
            device_arbitration=device_arbitration, previous_analysis=previous, config=self.config,
            scenario_results=scenario_results,
            detect_scenario_candidates=self.detect_scenario_candidates,
        )
        result: dict[str, Any] = {
            "schema_version": "global_analysis_result/2.0",
            "analysis_id": f"ga_{uuid4().hex}",
            "status": "succeeded" if device_health["status"] == "succeeded" else "insufficient_data",
            "scenario_type": scenario,
            "subject_id": subject,
            "analysis_window": {"task_limit": task_limit, "actual_task_count": len(data["device_tasks"])},
            "device_health_analysis": device_health,
            "packet_diagnosis_analysis": packet_diagnosis,
            "device_arbitration_analysis": device_arbitration,
            "physical_evidence_analysis": physical_evidence,
            "revision_deduplication": data.get("revision_deduplication", {}),
            "round_closure_analysis": data.get("round_closure_analysis", {}),
            "problem_candidates": candidates,
            "created_at_ns": time.time_ns(),
        }
        result.update(scenario_results)
        self.repository.save_result(result)
        return result


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value.strip()
