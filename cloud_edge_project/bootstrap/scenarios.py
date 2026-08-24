"""Single production assembly point for scenario plugins."""

from __future__ import annotations

from collections.abc import Iterable

from core.scenario_registry import ScenarioRegistry
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_PROVIDER,
    MODEL_UPDATE,
    STORAGE_PROVIDER,
    ScenarioPlugin,
)
from scenarios.bearing.plugin import BearingScenarioPlugin


EDGE_CAPABILITIES = frozenset(
    {EDGE_INFERENCE, MODEL_PROVIDER, CONSISTENCY_POLICY}
)
CLOUD_CAPABILITIES = frozenset(
    {
        CLOUD_DIAGNOSIS,
        GLOBAL_ANALYSIS,
        MODEL_UPDATE,
        ARBITRATION_POLICY,
        STORAGE_PROVIDER,
    }
)
SENDER_CAPABILITIES = frozenset({INPUT_ADAPTER})


def _build_registry(
    resolved_capabilities: frozenset[str] | None,
    plugins: Iterable[ScenarioPlugin] = (),
) -> ScenarioRegistry:
    registry = ScenarioRegistry()
    registry.register(BearingScenarioPlugin(resolved_capabilities))
    for plugin in plugins:
        registry.register(plugin)
    return registry


def build_scenario_registry(
    *, plugins: Iterable[ScenarioPlugin] = ()
) -> ScenarioRegistry:
    return _build_registry(None, plugins)


def build_edge_scenario_registry(
    *, plugins: Iterable[ScenarioPlugin] = ()
) -> ScenarioRegistry:
    return _build_registry(EDGE_CAPABILITIES, plugins)


def build_cloud_scenario_registry(
    *, plugins: Iterable[ScenarioPlugin] = ()
) -> ScenarioRegistry:
    return _build_registry(CLOUD_CAPABILITIES, plugins)


def build_sender_scenario_registry(
    *, plugins: Iterable[ScenarioPlugin] = ()
) -> ScenarioRegistry:
    return _build_registry(SENDER_CAPABILITIES, plugins)
