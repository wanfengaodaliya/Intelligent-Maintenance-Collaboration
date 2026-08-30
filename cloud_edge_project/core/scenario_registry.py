from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from core.scenario_errors import UnsupportedScenarioError
from core.scenario_plugin import CapabilityBinding, ScenarioManifest, ScenarioPlugin


class ScenarioRegistryError(ValueError):
    """Base error for instance-based scenario plugin registration."""


class DuplicateScenarioError(ScenarioRegistryError):
    def __init__(self, scenario_id: str):
        super().__init__(f"scenario_id is already registered: {scenario_id}")


class ScenarioNotFoundError(ScenarioRegistryError):
    def __init__(self, scenario_id: str):
        super().__init__(f"scenario_id is not registered: {scenario_id}")


class MissingScenarioCapabilityError(ScenarioRegistryError):
    def __init__(self, scenario_id: str, capability: str):
        super().__init__(
            f"scenario '{scenario_id}' does not provide capability '{capability}'"
        )


class UnresolvedScenarioCapabilityError(ScenarioRegistryError):
    def __init__(self, scenario_id: str, capability: str):
        super().__init__(
            f"scenario '{scenario_id}' capability '{capability}' has no executable provider"
        )


class InvalidScenarioPluginError(ScenarioRegistryError):
    pass


@dataclass(frozen=True)
class RegisteredScenario:
    manifest: ScenarioManifest
    capabilities: Mapping[str, CapabilityBinding]
    plugin: ScenarioPlugin

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capabilities",
            MappingProxyType(dict(self.capabilities)),
        )


class ScenarioRegistry:
    """Explicit registry for scenario plugins and their optional capabilities."""

    def __init__(self) -> None:
        self._plugins: dict[str, RegisteredScenario] = {}

    def register(self, plugin: ScenarioPlugin) -> None:
        manifest = plugin.manifest
        scenario_id = manifest.scenario_id
        if scenario_id in self._plugins:
            raise DuplicateScenarioError(scenario_id)
        bindings = dict(plugin.capabilities)
        declared = set(manifest.capabilities)
        provided = set(bindings)
        if declared != provided:
            missing = sorted(declared - provided)
            extra = sorted(provided - declared)
            details = []
            if missing:
                details.append(f"missing bindings: {', '.join(missing)}")
            if extra:
                details.append(f"undeclared bindings: {', '.join(extra)}")
            raise InvalidScenarioPluginError(
                f"invalid plugin '{scenario_id}': {'; '.join(details)}"
            )
        plugin.validate_configuration()
        self._plugins[scenario_id] = RegisteredScenario(
            manifest=manifest,
            capabilities=bindings,
            plugin=plugin,
        )

    def get(self, scenario_id: str) -> RegisteredScenario:
        try:
            return self._plugins[scenario_id]
        except KeyError as exc:
            raise ScenarioNotFoundError(scenario_id) from exc

    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def capabilities(self, scenario_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.get(scenario_id).manifest.capabilities))

    def get_capability(
        self,
        scenario_id: str,
        capability: str,
    ) -> CapabilityBinding:
        binding = self.get(scenario_id).capabilities.get(capability)
        if binding is None:
            raise MissingScenarioCapabilityError(scenario_id, capability)
        return binding

    def require_provider(self, scenario_id: str, capability: str) -> object:
        binding = self.get_capability(scenario_id, capability)
        if binding.provider is None:
            raise UnresolvedScenarioCapabilityError(scenario_id, capability)
        return binding.provider


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
        raise ValueError("scenario_type is required")
    scenario_type = str(value).strip().lower()
    if not scenario_type:
        raise ValueError("scenario_type is required")
    return scenario_type


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
