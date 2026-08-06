from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from core.scenario_errors import UnsupportedScenarioError
from scenarios.bearing.cloud.handler import BearingCloudHandler


DEFAULT_SCENARIO_TYPE = "bearing"


class ScenarioHandler(Protocol):
    scenario_type: str

    def infer(
        self,
        payload: dict[str, Any],
        *,
        context_transport: Any,
    ) -> dict[str, Any]: ...

    def run_enhanced_analysis(self, review_id: str) -> None: ...

    def get_final_summary(self, review_id: str) -> dict[str, Any] | None: ...


SCENARIO_HANDLERS = {"bearing": BearingCloudHandler}


def normalize_scenario_type(value: object) -> str:
    if value is None:
        return DEFAULT_SCENARIO_TYPE
    scenario_type = str(value).strip().lower()
    return scenario_type or DEFAULT_SCENARIO_TYPE


def get_scenario_handler(
    scenario_type: object,
    *,
    database_path: Path,
) -> ScenarioHandler:
    normalized = normalize_scenario_type(scenario_type)
    handler_type = SCENARIO_HANDLERS.get(normalized)
    if handler_type is None:
        raise UnsupportedScenarioError(normalized)
    return handler_type(database_path)
