"""Bearing-specific analyzer assembly for the generic global-analysis service."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

from core.scenario_plugin import GlobalAnalysisRuntime
from scenarios.bearing.cloud.global_analysis.analyzer import (
    build_bearing_maintenance_recommendations,
)
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import (
    analyze_bearing_aggregation,
)
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import (
    analyze_bearing_risk,
)
from scenarios.bearing.cloud.global_analysis.config import GlobalAnalysisConfig
from scenarios.bearing.cloud.global_analysis.problem_detector import (
    detect_bearing_problem_candidates,
)
from scenarios.bearing.cloud.global_analysis.v12_data_source import (
    V12GlobalAnalysisDataSource,
)


class BearingGlobalAnalysisProvider:
    scenario_id = "bearing"

    def build_analyzers(self) -> Mapping[str, Callable[..., Any]]:
        def risk(data: dict[str, Any], config: Any) -> dict[str, Any]:
            return analyze_bearing_risk(data.get("bearing_tasks", []), config)

        def review(data: dict[str, Any], config: Any) -> dict[str, Any]:
            rows = data.get("bearing_review_pairs", [])
            if not rows:
                return {"status": "not_available", "bearing_review_count": 0}
            return analyze_bearing_aggregation(rows, config)

        def maintenance(
            device_health: dict[str, Any],
            bearing_risk: dict[str, Any] | None,
        ) -> list[str]:
            if bearing_risk is None:
                return []
            return build_bearing_maintenance_recommendations(
                device_health,
                bearing_risk,
            )

        return {
            "analyze_bearing_risk": risk,
            "analyze_cloud_bearing_review": review,
            "maintenance_recommendations": maintenance,
        }

    def build_runtime(self, database_path: Path) -> GlobalAnalysisRuntime:
        analyzers = self.build_analyzers()
        return GlobalAnalysisRuntime(
            data_source=V12GlobalAnalysisDataSource(database_path),
            config=GlobalAnalysisConfig(),
            analyze_scenario=partial(_analyze_scenario_results, analyzers),
            detect_scenario_candidates=detect_bearing_problem_candidates,
        )


def _analyze_scenario_results(
    analyzers: Mapping[str, Callable[..., Any]],
    data: dict[str, Any],
    common_results: Mapping[str, Any],
    config: object,
) -> Mapping[str, Any]:
    results: dict[str, Any] = {}
    risk_fn = analyzers.get("analyze_bearing_risk")
    risk = risk_fn(data, config) if risk_fn is not None else None
    if risk is not None:
        results["bearing_risk_analysis"] = risk

    review_fn = analyzers.get("analyze_cloud_bearing_review")
    review = review_fn(data, config) if review_fn is not None else None
    if review is not None:
        results["cloud_bearing_review_analysis"] = {
            **review,
            "reviewed_bearing_count": review.get("bearing_review_count", 0),
        }

    maintenance_fn = analyzers.get("maintenance_recommendations")
    if maintenance_fn is not None:
        results["maintenance_recommendations"] = maintenance_fn(
            common_results["device_health_analysis"],
            risk,
        )
    return results
