from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bootstrap.scenarios import (
    build_cloud_scenario_registry,
    build_edge_scenario_registry,
    build_sender_scenario_registry,
    build_scenario_registry,
)
from core.scenario_contracts import ScenarioInferenceRequest
from core.scenario_plugin import ScenarioManifest
from core.scenario_registry import (
    DuplicateScenarioError,
    InvalidScenarioPluginError,
    MissingScenarioCapabilityError,
    ScenarioNotFoundError,
    ScenarioRegistry,
    UnresolvedScenarioCapabilityError,
)
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_PROVIDER,
    MODEL_UPDATE,
    CapabilityBinding,
)
from scenarios.bearing.manifest import BEARING_CAPABILITIES


class _Plugin:
    def __init__(self, scenario_id: str = "inspection") -> None:
        self.manifest = ScenarioManifest(
            scenario_id=scenario_id,
            version="1.0",
            capabilities=frozenset({"edge_inference"}),
        )
        self.capabilities = {
            "edge_inference": CapabilityBinding(
                capability="edge_inference",
                provider=object(),
            )
        }

    def validate_configuration(self) -> None:
        return None


def test_general_contract_is_validated_and_immutable() -> None:
    source_evidence = {"samples": [1.0, 2.0]}
    request = ScenarioInferenceRequest(
        scenario_id="inspection",
        task_id="task-1",
        unit_id="unit-1",
        device_id="edge-1",
        capability="edge_inference",
        observation_window_id="window-1",
        evidence=source_evidence,
    )

    assert request.unit_id == "unit-1"
    with pytest.raises(FrozenInstanceError):
        request.unit_id = "changed"  # type: ignore[misc]
    source_evidence["samples"].append(3.0)
    assert request.evidence["samples"] == (1.0, 2.0)
    with pytest.raises(TypeError):
        request.evidence["new"] = "value"  # type: ignore[index]


def test_registry_registers_bearing_plugin_and_exposes_all_capabilities() -> None:
    registry = build_scenario_registry()

    plugin = registry.get("bearing")

    assert plugin.manifest.scenario_id == "bearing"
    assert registry.scenario_ids() == ("bearing",)
    assert registry.capabilities("bearing") == tuple(sorted(BEARING_CAPABILITIES))
    for capability in BEARING_CAPABILITIES:
        binding = registry.get_capability("bearing", capability)
        assert binding.capability == capability
        assert binding.implementation_ref
        if capability in {
            EDGE_INFERENCE,
            INPUT_ADAPTER,
            MODEL_PROVIDER,
            CLOUD_DIAGNOSIS,
            GLOBAL_ANALYSIS,
            MODEL_UPDATE,
            CONSISTENCY_POLICY,
            ARBITRATION_POLICY,
        }:
            assert binding.resolved
            assert registry.require_provider("bearing", capability) is binding.provider
        else:
            assert not binding.resolved
            with pytest.raises(UnresolvedScenarioCapabilityError, match=capability):
                registry.require_provider("bearing", capability)


def test_role_scoped_registries_only_resolve_runtime_capabilities() -> None:
    edge_registry = build_edge_scenario_registry()
    cloud_registry = build_cloud_scenario_registry()
    sender_registry = build_sender_scenario_registry()

    for capability in {EDGE_INFERENCE, MODEL_PROVIDER, CONSISTENCY_POLICY}:
        assert edge_registry.get_capability("bearing", capability).resolved
        assert not cloud_registry.get_capability("bearing", capability).resolved
    for capability in {
        CLOUD_DIAGNOSIS,
        GLOBAL_ANALYSIS,
        MODEL_UPDATE,
        ARBITRATION_POLICY,
    }:
        assert cloud_registry.get_capability("bearing", capability).resolved
        assert not edge_registry.get_capability("bearing", capability).resolved
    assert sender_registry.get_capability("bearing", INPUT_ADAPTER).resolved
    for capability in {
        EDGE_INFERENCE,
        MODEL_PROVIDER,
        CLOUD_DIAGNOSIS,
        GLOBAL_ANALYSIS,
        MODEL_UPDATE,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
    }:
        assert not sender_registry.get_capability("bearing", capability).resolved


def test_registry_rejects_duplicate_scenario_id() -> None:
    registry = ScenarioRegistry()
    registry.register(_Plugin())

    with pytest.raises(DuplicateScenarioError, match="inspection"):
        registry.register(_Plugin())


def test_registry_returns_executable_provider_and_snapshots_bindings() -> None:
    registry = ScenarioRegistry()
    plugin = _Plugin()
    provider = plugin.capabilities["edge_inference"].provider
    registry.register(plugin)
    plugin.capabilities.clear()

    assert registry.require_provider("inspection", "edge_inference") is provider


def test_registry_uses_the_binding_snapshot_validated_before_plugin_hook() -> None:
    plugin = _Plugin()

    def _mutate_configuration() -> None:
        plugin.capabilities.clear()

    plugin.validate_configuration = _mutate_configuration  # type: ignore[method-assign]
    registry = ScenarioRegistry()
    registry.register(plugin)

    assert registry.get_capability("inspection", "edge_inference").resolved


def test_registry_reports_unknown_scenario_and_missing_capability() -> None:
    registry = ScenarioRegistry()
    registry.register(_Plugin())

    with pytest.raises(ScenarioNotFoundError, match="missing"):
        registry.get("missing")
    with pytest.raises(
        MissingScenarioCapabilityError,
        match="inspection.*cloud_diagnosis",
    ):
        registry.require_provider("inspection", "cloud_diagnosis")


def test_registry_rejects_manifest_provider_mismatch() -> None:
    plugin = _Plugin()
    plugin.capabilities = {}

    with pytest.raises(InvalidScenarioPluginError, match="edge_inference"):
        ScenarioRegistry().register(plugin)
