"""Periodic scheduling helpers for global analysis."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from cloud_service.global_analysis.runtime_contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.storage.database import connect
from core.scenario_plugin import GlobalAnalysisRuntime
from core.scenario_registry import DEFAULT_SCENARIO_TYPE

LOGGER = logging.getLogger(__name__)


def list_subject_ids(database_path: Path) -> list[str]:
    """Discover known device subjects from device decision records (empty when table is absent)."""
    with connect(database_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_device_decision_result'"
        ).fetchone()
        if not exists:
            return []
        rows = connection.execute(
            "SELECT DISTINCT device_id FROM cloud_device_decision_result ORDER BY device_id"
        ).fetchall()
        return [str(row["device_id"]) for row in rows]


def run_all(
    database_path: Path,
    scenario_type: str = DEFAULT_SCENARIO_TYPE,
    task_limit: int = DEFAULT_TASK_LIMIT,
    analyzers: dict[str, Callable[..., Any]] | None = None,
    *,
    runtime_factory: Callable[[Path], GlobalAnalysisRuntime] | None = None,
) -> list[str]:
    """Run a global analysis for every known subject, isolating per-subject failures.

    Returns the list of subject_ids whose analysis succeeded.
    """
    service = GlobalAnalysisService(
        database_path,
        runtime=runtime_factory(database_path) if runtime_factory else None,
        scenario_analyzers=analyzers,
    )
    succeeded: list[str] = []
    for subject_id in list_subject_ids(database_path):
        try:
            service.analyze(scenario_type, subject_id, task_limit)
            succeeded.append(subject_id)
        except Exception as exc:
            LOGGER.exception("periodic global analysis failed for %s: %s", subject_id, exc)
    return succeeded
