from types import MappingProxyType

import pytest

from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_UPDATE,
    STORAGE_PROVIDER,
)
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
    expected = {
        INPUT_ADAPTER,
        EDGE_INFERENCE,
        CLOUD_DIAGNOSIS,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
        STORAGE_PROVIDER,
    }
    assert REFERENCE_INSPECTION_CAPABILITIES == expected
    assert set(registry.capabilities("reference_inspection")) == expected
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
