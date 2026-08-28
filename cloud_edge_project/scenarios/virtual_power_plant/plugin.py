"""Capability bindings for the minimal virtual-power-plant scenario."""

from types import MappingProxyType
from typing import Mapping

from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    INPUT_ADAPTER,
    STORAGE_PROVIDER,
    CapabilityBinding,
)
from scenarios.virtual_power_plant.manifest import VPP_MANIFEST
from scenarios.virtual_power_plant.providers import (
    VppArbitrationPolicy,
    VppCloudDiagnosisProvider,
    VppConsistencyPolicy,
    VppEdgeInferenceProvider,
    VppInputAdapterProvider,
    VppStorageProvider,
)


class VirtualPowerPlantPlugin:
    manifest = VPP_MANIFEST

    def __init__(self) -> None:
        providers = {
            INPUT_ADAPTER: VppInputAdapterProvider(),
            EDGE_INFERENCE: VppEdgeInferenceProvider(),
            CLOUD_DIAGNOSIS: VppCloudDiagnosisProvider(),
            CONSISTENCY_POLICY: VppConsistencyPolicy(),
            ARBITRATION_POLICY: VppArbitrationPolicy(),
            STORAGE_PROVIDER: VppStorageProvider(),
        }
        self._capabilities = MappingProxyType(
            {
                capability: CapabilityBinding(
                    capability=capability,
                    provider=provider,
                    implementation_ref=(
                        "scenarios.virtual_power_plant.providers."
                        f"{type(provider).__name__}"
                    ),
                )
                for capability, provider in providers.items()
            }
        )

    @property
    def capabilities(self) -> Mapping[str, CapabilityBinding]:
        return self._capabilities

    def validate_configuration(self) -> None:
        if set(self._capabilities) != set(self.manifest.capabilities):
            raise ValueError("virtual power plant capabilities are incomplete")
