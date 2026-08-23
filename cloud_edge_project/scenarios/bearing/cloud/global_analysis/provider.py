"""Bearing-specific analyzer assembly for the generic global-analysis service."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from scenarios.bearing.cloud.global_analysis.analyzer import (
    build_bearing_maintenance_recommendations,
)
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import (
    analyze_bearing_aggregation,
)
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import (
    analyze_bearing_risk,
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
