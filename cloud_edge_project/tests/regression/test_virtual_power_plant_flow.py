from cloud_service.storage.database import connect, initialize_database
from core.arbitration_engine import ArbitrationEngine
from core.consistency_engine import ConsistencyEngine, ConsistencyRequest, ConsistencyUnit
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    STORAGE_PROVIDER,
)
from bootstrap.scenarios import build_scenario_registry
from scenarios.bearing.storage import BearingStorageProvider
from scenarios.virtual_power_plant import VirtualPowerPlantPlugin
from scenarios.virtual_power_plant.providers import aggregate_request


def _registry():
    return build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))


def test_vpp_cloud_dispatch_reduces_peak_to_grid_limit(tmp_path) -> None:
    handler = _registry().require_provider(
        "virtual_power_plant", CLOUD_DIAGNOSIS
    ).build_handler(tmp_path / "unused.db")

    result = handler.infer(aggregate_request())

    assert result["state"] == "peak_risk"
    assert result["evidence"]["original_load_kw"] == 1000.0
    assert result["evidence"]["planned_reduction_kw"] == 100.0
    assert result["evidence"]["post_dispatch_load_kw"] == 900.0
    assert result["evidence"]["allocation_kw"] == {
        "region_b": 80.0,
        "region_c": 20.0,
    }


def test_vpp_conflict_uses_generic_consistency_and_arbitration_engines() -> None:
    registry = _registry()
    policy = registry.require_provider("virtual_power_plant", CONSISTENCY_POLICY)
    decision = ConsistencyEngine(policy).evaluate(
        ConsistencyRequest(
            units=(
                ConsistencyUnit(
                    unit_id="edge-region-a",
                    lifecycle_status="FINAL",
                    confidence=0.9,
                    data_quality_score=1.0,
                    action_level=0,
                    scenario_payload={"state": "within_limit"},
                ),
                ConsistencyUnit(
                    unit_id="cloud-aggregate",
                    lifecycle_status="FINAL",
                    confidence=0.95,
                    data_quality_score=1.0,
                    action_level=3,
                    scenario_payload={"state": "peak_risk"},
                ),
            ),
            expected_unit_ids=("edge-region-a", "cloud-aggregate"),
            closure_reason="all_results_received",
            closed_at_ns=1,
        )
    )
    assert decision.has_conflict is True
    assert decision.final_action == "dispatch_reduction"

    arbitration = registry.require_provider("virtual_power_plant", ARBITRATION_POLICY)
    context = arbitration.build_context(
        {
            "scenario_type": "virtual_power_plant",
            "conflict_id": "conflict-1",
            "subject_id": "virtual-plant-1",
            "task_id": "task-1",
            "decision_units": [
                {
                    "unit_id": "edge-region-a",
                    "state": "within_limit",
                    "confidence": 0.9,
                    "data_quality_score": 1.0,
                    "risk_level": "low",
                    "recommended_action": "hold",
                },
                {
                    "unit_id": "cloud-aggregate",
                    "state": "peak_risk",
                    "confidence": 0.95,
                    "data_quality_score": 1.0,
                    "risk_level": "high",
                    "recommended_action": "dispatch_reduction",
                },
            ],
        }
    )
    result = ArbitrationEngine(
        arbitration,
        lambda *args, **kwargs: {"status": "unexpected"},
    ).decide(context)

    assert result["final_action"] == "dispatch_reduction"
    assert result["triggered_rule_id"] == "vpp-grid-limit-safety"


def test_vpp_storage_uses_existing_storage_boundary(tmp_path) -> None:
    storage = _registry().require_provider("virtual_power_plant", STORAGE_PROVIDER)
    database_path = tmp_path / "vpp.db"
    providers = (BearingStorageProvider(), storage)

    initialize_database(database_path, storage_providers=providers)
    initialize_database(database_path, storage_providers=providers)

    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='vpp_dispatch_result'"
        ).fetchone()
    assert row["name"] == "vpp_dispatch_result"
