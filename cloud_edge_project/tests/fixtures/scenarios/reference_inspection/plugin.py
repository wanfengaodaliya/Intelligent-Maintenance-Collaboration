"""Scenario declaration for the test-only reference inspection fixture."""

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
    ScenarioManifest,
)
from tests.fixtures.scenarios.reference_inspection.providers import (
    ReferenceArbitrationPolicy,
    ReferenceCloudDiagnosisProvider,
    ReferenceConsistencyPolicy,
    ReferenceEdgeInferenceProvider,
    ReferenceInputAdapterProvider,
    ReferenceStorageProvider,
)


REFERENCE_INSPECTION_CAPABILITIES = frozenset(
    {
        INPUT_ADAPTER,
        EDGE_INFERENCE,
        CLOUD_DIAGNOSIS,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
        STORAGE_PROVIDER,
    }
)

REFERENCE_INSPECTION_MANIFEST = ScenarioManifest(
    scenario_id="reference_inspection",
    version="test-1.0",
    capabilities=REFERENCE_INSPECTION_CAPABILITIES,
)


class ReferenceInspectionPlugin:
    manifest = REFERENCE_INSPECTION_MANIFEST

    def __init__(self) -> None:
        providers = {
            INPUT_ADAPTER: ReferenceInputAdapterProvider(),
            EDGE_INFERENCE: ReferenceEdgeInferenceProvider(),
            CLOUD_DIAGNOSIS: ReferenceCloudDiagnosisProvider(),
            CONSISTENCY_POLICY: ReferenceConsistencyPolicy(),
            ARBITRATION_POLICY: ReferenceArbitrationPolicy(),
            STORAGE_PROVIDER: ReferenceStorageProvider(),
        }
        self._capabilities = MappingProxyType(
            {
                capability: CapabilityBinding(
                    capability=capability,
                    provider=provider,
                    implementation_ref=(
                        "tests.fixtures.scenarios.reference_inspection.providers."
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
            raise ValueError("reference inspection capability declarations are incomplete")
