"""Bearing plugin declaration without changing existing runtime wiring."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    CLOUD_DIAGNOSIS,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_PROVIDER,
    MODEL_UPDATE,
    CapabilityBinding,
    ScenarioManifest,
)
from scenarios.bearing.manifest import BEARING_MANIFEST


_IMPLEMENTATION_AREAS = {
    "input_adapter": "scenarios.bearing.ingestion.BearingInputAdapterProvider",
    "edge_inference": "scenarios.bearing.edge",
    "cloud_diagnosis": "scenarios.bearing.cloud",
    "decision_policy": "edge_runtime.v12_flow",
    "consistency_policy": "scenarios.bearing.decision.BearingConsistencyPolicy",
    "arbitration_policy": "scenarios.bearing.arbitration.BearingArbitrationPolicy",
    "global_analysis": "scenarios.bearing.cloud.global_analysis",
    "model_provider": "scenarios.bearing.edge_inference.BearingEdgeModelProvider",
    "storage_provider": "cloud_service.storage",
    "model_update": "scenarios.bearing.cloud.model_update.BearingModelUpdateProvider",
}


class BearingScenarioPlugin:
    manifest: ScenarioManifest = BEARING_MANIFEST

    def __init__(self, resolved_capabilities: frozenset[str] | None = None) -> None:
        requested = (
            frozenset({
                INPUT_ADAPTER,
                EDGE_INFERENCE,
                MODEL_PROVIDER,
                CLOUD_DIAGNOSIS,
                GLOBAL_ANALYSIS,
                MODEL_UPDATE,
                CONSISTENCY_POLICY,
                ARBITRATION_POLICY,
            })
            if resolved_capabilities is None
            else resolved_capabilities
        )
        resolved_providers: dict[str, object] = {}
        if requested.intersection({EDGE_INFERENCE, MODEL_PROVIDER}):
            from scenarios.bearing.edge_inference import (
                BearingEdgeInferenceProvider,
                BearingEdgeModelProvider,
            )

            model_provider = BearingEdgeModelProvider()
            resolved_providers[MODEL_PROVIDER] = model_provider
            resolved_providers[EDGE_INFERENCE] = BearingEdgeInferenceProvider(
                model_provider
            )
        if INPUT_ADAPTER in requested:
            from scenarios.bearing.ingestion import BearingInputAdapterProvider

            resolved_providers[INPUT_ADAPTER] = BearingInputAdapterProvider()
        if CLOUD_DIAGNOSIS in requested:
            from scenarios.bearing.cloud_diagnosis import (
                BearingCloudDiagnosisProvider,
            )

            resolved_providers[CLOUD_DIAGNOSIS] = BearingCloudDiagnosisProvider()
        if GLOBAL_ANALYSIS in requested:
            from scenarios.bearing.cloud.global_analysis.provider import (
                BearingGlobalAnalysisProvider,
            )

            resolved_providers[GLOBAL_ANALYSIS] = BearingGlobalAnalysisProvider()
        if MODEL_UPDATE in requested:
            from scenarios.bearing.cloud.model_update.provider import (
                BearingModelUpdateProvider,
            )

            resolved_providers[MODEL_UPDATE] = BearingModelUpdateProvider()
        if CONSISTENCY_POLICY in requested:
            from scenarios.bearing.decision import BearingConsistencyPolicy

            resolved_providers[CONSISTENCY_POLICY] = BearingConsistencyPolicy()
        if ARBITRATION_POLICY in requested:
            from scenarios.bearing.arbitration import BearingArbitrationPolicy

            resolved_providers[ARBITRATION_POLICY] = BearingArbitrationPolicy()
        self._capabilities = MappingProxyType(
            {
                capability: (
                    CapabilityBinding(
                        capability=capability,
                        provider=resolved_providers[capability],
                        implementation_ref=implementation_area,
                    )
                    if capability in resolved_providers
                    else CapabilityBinding(
                        capability=capability,
                        implementation_ref=implementation_area,
                    )
                )
                for capability, implementation_area in _IMPLEMENTATION_AREAS.items()
            }
        )

    @property
    def capabilities(self) -> Mapping[str, CapabilityBinding]:
        return self._capabilities

    def validate_configuration(self) -> None:
        if set(self._capabilities) != set(self.manifest.capabilities):
            raise ValueError("bearing capability declarations are incomplete")
