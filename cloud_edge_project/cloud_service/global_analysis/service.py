"""Orchestration for read-only historical global analysis.

Generic service that orchestrates data loading, analysis, and storage.
Scenario-specific analysis functions are injected from outside (e.g. app.py).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from cloud_service.global_analysis.arbitration_analyzer import analyze_device_arbitration
from cloud_service.global_analysis.contracts import DEFAULT_TASK_LIMIT, GlobalAnalysisConfig
from cloud_service.global_analysis.device_health_analyzer import analyze_device_health
from cloud_service.global_analysis.packet_model_analyzer import analyze_packet_model
from cloud_service.global_analysis.physical_evidence_analyzer import analyze_physical_evidence
from cloud_service.global_analysis.problem_detector import detect_problem_candidates
from cloud_service.global_analysis.result_repository import GlobalAnalysisResultRepository
from cloud_service.global_analysis.v12_data_source import V12GlobalAnalysisDataSource


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
        config: GlobalAnalysisConfig | None = None,
        *,
        # Optional scenario-specific analysis callables.
        # Each receives (data, config) and returns a dict.
        # Defaults to None = skip that analysis step.
        scenario_analyzers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.repository = GlobalAnalysisResultRepository(self.database_path)
        self.data_source = data_source or V12GlobalAnalysisDataSource(self.database_path)
        self.config = config or GlobalAnalysisConfig()
        self.scenario_analyzers = dict(scenario_analyzers or {})

    def analyze(self, scenario_type: str, subject_id: str, task_limit: int = DEFAULT_TASK_LIMIT) -> dict[str, Any]:
        scenario = _required_identifier(scenario_type, "scenario_type")
        subject = _required_identifier(subject_id, "subject_id")
        if isinstance(task_limit, bool) or not isinstance(task_limit, int) or task_limit < 1:
            raise ValueError("task_limit must be a positive integer")
        data = self.data_source.load(subject, task_limit)
        availability = data.get("availability", {})
        device_health = analyze_device_health(data["device_tasks"], self.config)
        # Run scenario-specific analysis if a callable is registered
        bearing_risk = self._run_analyzer("analyze_bearing_risk", data, self.config)
        packet_diagnosis = analyze_packet_model(
            data["packet_review_pairs"], self.config,
            available=availability.get("packet_review_pairs", True),
        )
        cloud_bearing_review = self._run_analyzer("analyze_cloud_bearing_review", data, self.config)
        device_arbitration = analyze_device_arbitration(
            data.get("summary_windows", []), data["arbitrations"], self.config
        )
        physical_evidence = analyze_physical_evidence(
            data.get("physical_evidence", []),
            edge_summary_count=len(data.get("edge_summaries", [])),
            available=availability.get("physical_evidence", False),
        )
        previous = self.repository.get_recent(scenario, subject, 3)
        candidates = detect_problem_candidates(
            device_health=device_health, bearing_risk=bearing_risk,
            packet_diagnosis=packet_diagnosis, cloud_bearing_review=cloud_bearing_review,
            device_arbitration=device_arbitration, previous_analysis=previous, config=self.config,
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
        # Only add bearing-specific fields when the analyzer was provided
        if bearing_risk is not None:
            result["bearing_risk_analysis"] = bearing_risk
        if cloud_bearing_review is not None:
            result["cloud_bearing_review_analysis"] = {
                **cloud_bearing_review,
                "reviewed_bearing_count": cloud_bearing_review.get("bearing_review_count", 0),
            }
        # Run maintenance recommendations if registered
        maintenance_fn = self.scenario_analyzers.get("maintenance_recommendations")
        if maintenance_fn is not None:
            result["maintenance_recommendations"] = maintenance_fn(device_health, bearing_risk)
        self.repository.save_result(result)
        return result

    def _run_analyzer(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = self.scenario_analyzers.get(name)
        if fn is None:
            return None
        return fn(*args, **kwargs)


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value.strip()
