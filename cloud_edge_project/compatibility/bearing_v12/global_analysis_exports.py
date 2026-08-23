"""Explicit V1.2 exports for bearing global-analysis compatibility."""

from pathlib import Path
from typing import Any, Mapping

from cloud_service.global_analysis.runtime_contracts import DEFAULT_TASK_LIMIT
from core.scenario_plugin import GlobalAnalysisRuntime
from scenarios.bearing.cloud.global_analysis.config import (
    DEFAULT_CONFIG,
    GlobalAnalysisConfig,
)
from scenarios.bearing.cloud.global_analysis.v12_data_source import (
    V12GlobalAnalysisDataSource,
)


def build_legacy_global_analysis_runtime(
    database_path: Path,
    *,
    data_source: Any | None = None,
    config: Any | None = None,
    scenario_analyzers: Mapping[str, Any] | None = None,
) -> GlobalAnalysisRuntime:
    from scenarios.bearing.cloud.global_analysis.problem_detector import (
        detect_bearing_problem_candidates,
    )
    from scenarios.bearing.cloud.global_analysis.provider import (
        _analyze_scenario_results,
    )

    analyzers = dict(scenario_analyzers or {})

    def analyze_scenario(
        data: dict[str, Any],
        common_results: Mapping[str, Any],
        runtime_config: object,
    ) -> Mapping[str, Any]:
        if not analyzers:
            return {}
        return _analyze_scenario_results(
            analyzers,
            data,
            common_results,
            runtime_config,
        )

    def detect_scenario_candidates(
        analysis_results: Mapping[str, Any],
        previous_analysis: list[dict[str, Any]],
        runtime_config: object,
    ) -> list[dict[str, Any]]:
        if not analyzers:
            return []
        return detect_bearing_problem_candidates(
            analysis_results,
            previous_analysis,
            runtime_config,
        )

    return GlobalAnalysisRuntime(
        data_source=data_source or V12GlobalAnalysisDataSource(database_path),
        config=config or DEFAULT_CONFIG,
        analyze_scenario=analyze_scenario,
        detect_scenario_candidates=detect_scenario_candidates,
    )


def detect_legacy_scenario_candidates(
    legacy_inputs: Mapping[str, Any],
    previous_analysis: list[dict[str, Any]],
    config: Any,
) -> list[dict[str, Any]]:
    from scenarios.bearing.cloud.global_analysis.problem_detector import (
        detect_bearing_problem_candidates,
    )

    review = legacy_inputs.get("cloud_bearing_review")
    analysis_results = (
        {"cloud_bearing_review_analysis": review}
        if review is not None
        else {}
    )
    return detect_bearing_problem_candidates(
        analysis_results,
        previous_analysis,
        config,
    )


__all__ = [
    "GlobalAnalysisConfig",
    "DEFAULT_CONFIG",
    "DEFAULT_TASK_LIMIT",
    "V12GlobalAnalysisDataSource",
    "build_legacy_global_analysis_runtime",
]
