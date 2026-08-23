from types import MappingProxyType

import pytest

from core.scenario_plugin import GLOBAL_ANALYSIS, MODEL_UPDATE
from core.scenario_registry import MissingScenarioCapabilityError, ScenarioRegistry
from tests.fixtures.scenarios.reference_inspection import (
    REFERENCE_INSPECTION_CAPABILITIES,
    ReferenceInspectionPlugin,
)


def _registry() -> ScenarioRegistry:
    registry = ScenarioRegistry()
    registry.register(ReferenceInspectionPlugin())
    return registry


def test_reference_plugin_registers_exactly_six_resolved_capabilities() -> None:
    registry = _registry()

    assert registry.scenario_ids() == ("reference_inspection",)
    assert len(REFERENCE_INSPECTION_CAPABILITIES) == 6
    assert set(registry.capabilities("reference_inspection")) == set(
        REFERENCE_INSPECTION_CAPABILITIES
    )
    for capability in REFERENCE_INSPECTION_CAPABILITIES:
        binding = registry.get_capability("reference_inspection", capability)
        assert binding.resolved
        assert registry.require_provider("reference_inspection", capability) is binding.provider


def test_reference_plugin_keeps_bindings_immutable() -> None:
    plugin = ReferenceInspectionPlugin()

    assert isinstance(plugin.capabilities, MappingProxyType)
    with pytest.raises(TypeError):
        plugin.capabilities["extra"] = object()


@pytest.mark.parametrize("capability", [MODEL_UPDATE, GLOBAL_ANALYSIS])
def test_reference_plugin_omits_unneeded_optional_capabilities(capability: str) -> None:
    registry = _registry()

    with pytest.raises(MissingScenarioCapabilityError, match=capability):
        registry.require_provider("reference_inspection", capability)
