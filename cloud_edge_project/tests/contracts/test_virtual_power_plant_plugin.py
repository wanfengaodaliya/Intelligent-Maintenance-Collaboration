from pathlib import Path

import pytest

from bootstrap.scenarios import build_scenario_registry
from core.scenario_plugin import (
    ARBITRATION_POLICY,
    CLOUD_DIAGNOSIS,
    CONSISTENCY_POLICY,
    EDGE_INFERENCE,
    INPUT_ADAPTER,
    STORAGE_PROVIDER,
)
from scenarios.virtual_power_plant import VirtualPowerPlantPlugin


def test_vpp_plugin_registers_all_minimal_capabilities() -> None:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))

    assert registry.scenario_ids() == ("bearing", "virtual_power_plant")
    for capability in (
        INPUT_ADAPTER,
        EDGE_INFERENCE,
        CLOUD_DIAGNOSIS,
        CONSISTENCY_POLICY,
        ARBITRATION_POLICY,
        STORAGE_PROVIDER,
    ):
        assert registry.require_provider("virtual_power_plant", capability) is not None


def test_vpp_input_adapter_emits_generic_energy_packet(tmp_path: Path) -> None:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))
    adapter = registry.require_provider(
        "virtual_power_plant", INPUT_ADAPTER
    ).build_adapter(tmp_path)
    prepared = adapter.prepare(
        tmp_path / "virtual.input", unit_id="region_a", duration_ms=50, count=1
    )
    window = adapter.next_window(prepared, unit_id="region_a", expected_sequence=1)
    packet = adapter.build_packet(
        device_id="meter-a",
        task_id="task-1",
        unit_id="region_a",
        sender_id="sender-a",
        sequence_number=1,
        window=window,
        end_generate_timestamp_ns=1,
    )

    assert packet["scenario_id"] == "virtual_power_plant"
    assert packet["evidence"]["net_load_kw"] == 320.0
    assert "bearing" not in repr(packet).lower()


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"net_load_kw": -1, "local_limit_kw": 100, "available_reduction_kw": 1},
        {"net_load_kw": 1, "local_limit_kw": 0, "available_reduction_kw": 1},
        {"net_load_kw": float("nan"), "local_limit_kw": 100, "available_reduction_kw": 1},
        {"net_load_kw": 1, "local_limit_kw": float("inf"), "available_reduction_kw": 1},
    ],
)
def test_vpp_edge_rejects_invalid_energy_values(evidence: dict) -> None:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))
    provider = registry.require_provider("virtual_power_plant", EDGE_INFERENCE)
    payload = {
        "scenario_id": "virtual_power_plant",
        "task_id": "task-1",
        "unit_id": "region-a",
        "device_id": "meter-a",
        "capability": "edge_inference",
        "observation_window_id": "window-1",
        "evidence": evidence,
    }

    with pytest.raises(ValueError, match="INVALID_VPP_REQUEST"):
        provider.infer_compatible(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "regions": [
                {"region_id": "a", "net_load_kw": 10, "available_reduction_kw": 11}
            ]
        },
        {
            "regions": [
                {"region_id": "a", "net_load_kw": 10, "available_reduction_kw": 1},
                {"region_id": "a", "net_load_kw": 20, "available_reduction_kw": 2},
            ]
        },
    ],
)
def test_vpp_cloud_rejects_impossible_or_duplicate_regions(
    tmp_path: Path,
    changes: dict,
) -> None:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))
    handler = registry.require_provider(
        "virtual_power_plant", CLOUD_DIAGNOSIS
    ).build_handler(tmp_path / "unused.db")
    payload = {
        "scenario_id": "virtual_power_plant",
        "task_id": "task-1",
        "unit_id": "plant-1",
        "device_id": "aggregator-1",
        "capability": "cloud_diagnosis",
        "observation_window_id": "window-1",
        "evidence": {"grid_limit_kw": 100, **changes},
    }

    with pytest.raises(ValueError, match="INVALID_VPP_REQUEST"):
        handler.infer(payload)


@pytest.mark.parametrize(
    ("state", "action"),
    [("unknown", "hold"), ("within_limit", "unknown")],
)
def test_vpp_arbitration_rejects_unknown_state_or_action(
    state: str,
    action: str,
) -> None:
    registry = build_scenario_registry(plugins=(VirtualPowerPlantPlugin(),))
    policy = registry.require_provider("virtual_power_plant", ARBITRATION_POLICY)
    request = {
        "scenario_type": "virtual_power_plant",
        "conflict_id": "conflict-1",
        "subject_id": "plant-1",
        "task_id": "task-1",
        "decision_units": [
            {
                "unit_id": "unit-1",
                "state": state,
                "confidence": 0.9,
                "data_quality_score": 1.0,
                "risk_level": "low",
                "recommended_action": action,
            }
        ],
    }

    with pytest.raises(ValueError, match="INVALID_VPP_ARBITRATION"):
        policy.build_context(request)
