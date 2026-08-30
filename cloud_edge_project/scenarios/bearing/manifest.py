"""Bearing scenario metadata and declared platform capabilities."""

from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    DECISION_POLICY,
    EDGE_INFERENCE,
    GLOBAL_ANALYSIS,
    INPUT_ADAPTER,
    MODEL_PROVIDER,
    MODEL_UPDATE,
    STORAGE_PROVIDER,
    ScenarioManifest,
)


BEARING_CAPABILITIES = frozenset(
    {
        INPUT_ADAPTER,
        EDGE_INFERENCE,
        CLOUD_DIAGNOSIS,
        DECISION_POLICY,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
        GLOBAL_ANALYSIS,
        MODEL_PROVIDER,
        MODEL_UPDATE,
        STORAGE_PROVIDER,
    }
)

BEARING_MANIFEST = ScenarioManifest(
    scenario_id="bearing",
    version="1.0",
    capabilities=BEARING_CAPABILITIES,
)
