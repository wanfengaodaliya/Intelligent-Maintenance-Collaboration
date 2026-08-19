from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from core.scenario_errors import UnsupportedScenarioError


DEFAULT_SCENARIO_TYPE = "bearing"


class ScenarioHandler(Protocol):
    scenario_type: str

    def infer(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def arbitrate_device_conflict(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_device_arbitration(self, conflict_id: str) -> dict[str, Any] | None: ...


# Mutable registry: handlers are registered from outside (e.g. cloud_service/app.py).
# core does NOT import any scenario implementation.
_SCENARIO_HANDLERS: dict[str, type] = {}


def register_handler(scenario_type: str, handler_class: type) -> None:
    """Register a scenario handler class for the given scenario_type."""
    _SCENARIO_HANDLERS[scenario_type] = handler_class


def get_registered_types() -> tuple[str, ...]:
    """Return the list of registered scenario types."""
    return tuple(_SCENARIO_HANDLERS.keys())


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
    handler_type = _SCENARIO_HANDLERS.get(normalized)
    if handler_type is None:
        raise UnsupportedScenarioError(normalized)
    return handler_type(database_path)
