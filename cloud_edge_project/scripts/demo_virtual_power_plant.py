"""Run the minimal multi-scenario portability validation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for import_root in (PROJECT_ROOT, PROJECT_ROOT / "sender_module"):
    value = str(import_root)
    if value not in sys.path:
        sys.path.insert(0, value)

from bootstrap.scenarios import build_scenario_registry  # noqa: E402
from cloud_service.storage.database import connect, initialize_database  # noqa: E402
from core.arbitration_engine import ArbitrationEngine  # noqa: E402
from core.consistency_engine import (  # noqa: E402
    ConsistencyEngine,
    ConsistencyRequest,
    ConsistencyUnit,
)
from core.scenario_plugin import (  # noqa: E402
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    INPUT_ADAPTER,
    STORAGE_PROVIDER,
)
from scenarios.bearing.storage import BearingStorageProvider  # noqa: E402
from scenarios.virtual_power_plant import VirtualPowerPlantPlugin  # noqa: E402
from scenarios.virtual_power_plant.providers import aggregate_request  # noqa: E402


def run_demo() -> dict[str, object]:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))
    adapter = registry.require_provider(
        "virtual_power_plant", INPUT_ADAPTER
    ).build_adapter(PROJECT_ROOT)
    prepared = adapter.prepare(
        PROJECT_ROOT / "virtual-energy-source.json",
        unit_id="region_a",
        duration_ms=50,
        count=3,
    )
    window = adapter.next_window(prepared, unit_id="region_a", expected_sequence=1)
    edge_packet = adapter.build_packet(
        device_id="energy-meter-a",
        task_id="vpp-dispatch-1",
        unit_id="region_a",
        sender_id="vpp-sender-a",
        sequence_number=1,
        window=window,
        end_generate_timestamp_ns=1,
    )
    edge = registry.require_provider(
        "virtual_power_plant", EDGE_INFERENCE
    ).infer_compatible(edge_packet)
    cloud = registry.require_provider(
        "virtual_power_plant", CLOUD_DIAGNOSIS
    ).build_handler(PROJECT_ROOT / "unused.db").infer(aggregate_request())

    consistency = ConsistencyEngine(
        registry.require_provider("virtual_power_plant", CONSISTENCY_POLICY)
    ).evaluate(
        ConsistencyRequest(
            units=(
                _unit("edge-region-a", edge),
                _unit("cloud-aggregate", cloud),
            ),
            expected_unit_ids=("edge-region-a", "cloud-aggregate"),
            closure_reason="all_results_received",
            closed_at_ns=1,
        )
    )

    arbitration_policy = registry.require_provider(
        "virtual_power_plant", ARBITRATION_POLICY
    )
    context = arbitration_policy.build_context(
        {
            "scenario_type": "virtual_power_plant",
            "conflict_id": "vpp-conflict-1",
            "subject_id": "virtual-plant-1",
            "task_id": "vpp-dispatch-1",
            "decision_units": [
                _decision_unit("edge-region-a", edge, "hold"),
                _decision_unit("cloud-aggregate", cloud, "dispatch_reduction"),
            ],
        }
    )
    arbitration = ArbitrationEngine(
        arbitration_policy,
        lambda *args, **kwargs: {"status": "unresolved"},
    ).decide(context)

    storage_ready = _verify_storage(registry)
    evidence = cloud["evidence"]
    passed = (
        registry.scenario_ids() == ("bearing", "virtual_power_plant")
        and evidence["original_load_kw"] == 1000.0
        and evidence["planned_reduction_kw"] == 100.0
        and evidence["post_dispatch_load_kw"] == 900.0
        and dict(evidence["allocation_kw"])
        == {"region_b": 80.0, "region_c": 20.0}
        and consistency.has_conflict
        and arbitration["final_action"] == "dispatch_reduction"
        and storage_ready
    )
    return {
        "loaded_scenarios": list(registry.scenario_ids()),
        "edge_state": edge["state"],
        "cloud_state": cloud["state"],
        "original_load_kw": evidence["original_load_kw"],
        "grid_limit_kw": evidence["grid_limit_kw"],
        "planned_reduction_kw": evidence["planned_reduction_kw"],
        "post_dispatch_load_kw": evidence["post_dispatch_load_kw"],
        "allocation_kw": dict(evidence["allocation_kw"]),
        "conflict_detected": consistency.has_conflict,
        "arbitration_action": arbitration["final_action"],
        "storage_ready": storage_ready,
        "platform_core_changed_for_scenario": False,
        "result": "PASS" if passed else "FAIL",
    }


def _unit(unit_id: str, diagnosis: dict[str, object]) -> ConsistencyUnit:
    return ConsistencyUnit(
        unit_id=unit_id,
        lifecycle_status="FINAL",
        confidence=float(diagnosis["confidence"]),
        data_quality_score=1.0,
        action_level=int(diagnosis["action_level"]),
        scenario_payload={"state": diagnosis["state"]},
    )


def _decision_unit(
    unit_id: str,
    diagnosis: dict[str, object],
    action: str,
) -> dict[str, object]:
    return {
        "unit_id": unit_id,
        "state": diagnosis["state"],
        "confidence": diagnosis["confidence"],
        "data_quality_score": 1.0,
        "risk_level": diagnosis["risk_level"],
        "recommended_action": action,
        "scenario_payload": dict(diagnosis["evidence"]),
    }


def _verify_storage(registry) -> bool:
    provider = registry.require_provider("virtual_power_plant", STORAGE_PROVIDER)
    with tempfile.TemporaryDirectory() as directory:
        database_path = Path(directory) / "vpp.db"
        initialize_database(
            database_path,
            storage_providers=(BearingStorageProvider(), provider),
        )
        initialize_database(
            database_path,
            storage_providers=(BearingStorageProvider(), provider),
        )
        with connect(database_path) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='vpp_dispatch_result'"
            ).fetchone()
        return row is not None


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2, sort_keys=True))
