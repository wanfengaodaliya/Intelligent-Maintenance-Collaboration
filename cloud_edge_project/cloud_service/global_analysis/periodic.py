"""Periodic scheduling helpers for global analysis."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from cloud_service.global_analysis.runtime_contracts import DEFAULT_TASK_LIMIT
from cloud_service.global_analysis.service import GlobalAnalysisService
from cloud_service.storage.database import connect
from core.scenario_plugin import GlobalAnalysisRuntime
from compatibility.bearing_v12.scenario_mapper import normalize_legacy_scenario_type

LOGGER = logging.getLogger(__name__)


def list_subject_ids(database_path: Path) -> list[str]:
    """Discover devices from formal Summary windows and retained decision records."""
    with connect(database_path) as connection:
        device_ids: set[str] = set()
        for table in ("summary_window_record", "cloud_device_decision_result"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                rows = connection.execute(
                    f"SELECT DISTINCT device_id FROM {table}"
                ).fetchall()
                device_ids.update(str(row["device_id"]) for row in rows)
        return sorted(device_ids)


def run_all(
    database_path: Path,
    scenario_type: str | None = None,
    task_limit: int = DEFAULT_TASK_LIMIT,
    analyzers: dict[str, Callable[..., Any]] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
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
            result = service.analyze(
                normalize_legacy_scenario_type(scenario_type),
                subject_id,
                task_limit,
            )
            succeeded.append(subject_id)
        except Exception as exc:
            LOGGER.exception("periodic global analysis failed for %s: %s", subject_id, exc)
            continue
        if on_result is not None:
            try:
                on_result(result)
            except Exception as exc:
                LOGGER.exception(
                    "post-analysis callback failed for %s: %s", subject_id, exc
                )
    return succeeded
