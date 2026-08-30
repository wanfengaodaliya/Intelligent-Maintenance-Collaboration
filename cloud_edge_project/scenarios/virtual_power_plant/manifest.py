"""Virtual-power-plant scenario metadata."""

from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    INPUT_ADAPTER,
    STORAGE_PROVIDER,
    ScenarioManifest,
)


VPP_CAPABILITIES = frozenset(
    {
        INPUT_ADAPTER,
        EDGE_INFERENCE,
        CLOUD_DIAGNOSIS,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
        STORAGE_PROVIDER,
    }
)

VPP_MANIFEST = ScenarioManifest(
    scenario_id="virtual_power_plant",
    version="validation-1.0",
    capabilities=VPP_CAPABILITIES,
)
