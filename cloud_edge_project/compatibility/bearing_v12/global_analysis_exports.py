"""Explicit V1.2 exports for bearing global-analysis compatibility."""

from pathlib import Path
from typing import Any

from cloud_service.global_analysis.runtime_contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.v12_data_source import (
    V12GlobalAnalysisDataSource,
)
from scenarios.bearing.cloud.global_analysis.config import (
    DEFAULT_CONFIG,
    GlobalAnalysisConfig,
)


def build_legacy_global_analysis_runtime(database_path: Path) -> Any:
    from scenarios.bearing.cloud.global_analysis.provider import (
        BearingGlobalAnalysisProvider,
    )

    return BearingGlobalAnalysisProvider().build_runtime(database_path)


__all__ = [
    "GlobalAnalysisConfig",
    "DEFAULT_CONFIG",
    "DEFAULT_TASK_LIMIT",
    "V12GlobalAnalysisDataSource",
    "build_legacy_global_analysis_runtime",
]
