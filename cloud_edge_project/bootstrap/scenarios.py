"""Single production assembly point for scenario plugins."""

from __future__ import annotations

from core.scenario_registry import ScenarioRegistry
from core.scenario_plugin import (
    CLOUD_DIAGNOSIS,
    EDGE_INFERENCE,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_PROVIDER,
    MODEL_UPDATE,
)
from scenarios.bearing.plugin import BearingScenarioPlugin


EDGE_CAPABILITIES = frozenset({EDGE_INFERENCE, MODEL_PROVIDER})
CLOUD_CAPABILITIES = frozenset(
    {CLOUD_DIAGNOSIS, GLOBAL_ANALYSIS, MODEL_UPDATE}
)
SENDER_CAPABILITIES = frozenset({INPUT_ADAPTER})


def _build_registry(resolved_capabilities: frozenset[str] | None) -> ScenarioRegistry:
    registry = ScenarioRegistry()
    registry.register(BearingScenarioPlugin(resolved_capabilities))
    return registry


def build_scenario_registry() -> ScenarioRegistry:
    return _build_registry(None)


def build_edge_scenario_registry() -> ScenarioRegistry:
    return _build_registry(EDGE_CAPABILITIES)


def build_cloud_scenario_registry() -> ScenarioRegistry:
    return _build_registry(CLOUD_CAPABILITIES)


def build_sender_scenario_registry() -> ScenarioRegistry:
    return _build_registry(SENDER_CAPABILITIES)
