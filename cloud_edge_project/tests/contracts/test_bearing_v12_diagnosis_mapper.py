from dataclasses import replace

import pytest

from compatibility.bearing_v12.diagnosis_mapper import (
    bearing_decision_to_scenario,
    cloud_bearing_to_scenario,
    device_decision_to_scenario,
    edge_bearing_to_scenario,
    scenario_to_bearing_decision,
    scenario_to_cloud_bearing,
    scenario_to_device_decision,
    scenario_to_edge_bearing,
)
from core.diagnosis_contracts import (
    BearingDecisionResult,
    BearingLifecycleStatus,
    CloudBearingResult,
    DeviceDecisionResult,
    DeviceDecisionStatus,
    EdgeBearingResult,
    RoundClosureReason,
)


def _edge() -> EdgeBearingResult:
    return EdgeBearingResult(
        "edge_1", "device_1", "task_1", "bearing_1", "sender_1", "round_1",
        "window_1", 1, 8, 10, 20, ("packet_1",), "inner_race_fault", 0.91,
        0.88, "high", 3, "inspect", "edge-v1", 30, "IR", {"IR": 0.91},
    )


def _cloud() -> CloudBearingResult:
    return CloudBearingResult(
        "cloud_1", "review_1", "device_1", "task_1", "bearing_1", "sender_1",
        "round_1", "window_1", 1, 8, 10, 20, "normal", 0.95, 0.92, "low",
        0, "continue", "cloud-v1", 31,
    )


def _bearing_decision() -> BearingDecisionResult:
    return BearingDecisionResult(
        "decision_1", 1, None, "device_1", "task_1", "bearing_1", "sender_1",
        "round_1", "window_1", BearingLifecycleStatus.FINAL_CLOUD, "normal", 0.95,
        0.92, "low", 0, "continue", "cloud", "REVIEWED", False, "edge_1",
        "cloud_1", "cloud-v1", 32, 30,
    )


def _device_decision() -> DeviceDecisionResult:
    return DeviceDecisionResult(
        "device_decision_1", 1, None, "device_1", "task_1", "round_1",
        ("bearing_1",), ("bearing_1",), (), ("decision_1",),
        DeviceDecisionStatus.FINAL, RoundClosureReason.ALL_BEARINGS_FINAL,
        "normal", 0, "continue", 0.95, 0.92, False, (), "rule", False,
        True, None, 40, 40,
    )


@pytest.mark.parametrize(
    ("legacy", "to_scenario", "from_scenario", "model_id"),
    [
        (_edge(), edge_bearing_to_scenario, scenario_to_edge_bearing, "distilled_h5"),
        (_cloud(), cloud_bearing_to_scenario, scenario_to_cloud_bearing, "moment_light_adapt"),
    ],
)
def test_diagnosis_mapping_roundtrip(legacy, to_scenario, from_scenario, model_id) -> None:
    scenario = to_scenario(legacy)

    assert scenario.model_id == model_id
    assert scenario.evidence["data_quality_score"] == legacy.data_quality_score
    assert from_scenario(scenario, legacy) == legacy


def test_bearing_decision_mapping_roundtrip() -> None:
    legacy = _bearing_decision()
    scenario = bearing_decision_to_scenario(legacy)

    assert scenario.decision == "continue"
    assert scenario.evidence["model_version"] == "cloud-v1"
    assert scenario_to_bearing_decision(scenario, legacy) == legacy


def test_device_decision_mapping_roundtrip() -> None:
    legacy = _device_decision()
    scenario = device_decision_to_scenario(legacy)

    assert scenario.unit_id == "device_1"
    assert scenario_to_device_decision(scenario, legacy) == legacy


def test_reverse_mapping_updates_only_shared_conclusion_fields() -> None:
    legacy = _edge()
    scenario = replace(
        edge_bearing_to_scenario(legacy),
        state="outer_race_fault",
        confidence=0.8,
        risk_level="medium",
        action_level=2,
        model_version="edge-v2",
    )

    updated = scenario_to_edge_bearing(scenario, legacy)

    assert updated.bearing_state == "outer_race_fault"
    assert updated.model_version == "edge-v2"
    assert updated.result_id == legacy.result_id
    assert updated.diagnosis_window_id == legacy.diagnosis_window_id


def test_reverse_mapping_rejects_wrong_template_type() -> None:
    with pytest.raises(TypeError, match="EdgeBearingResult"):
        scenario_to_edge_bearing(edge_bearing_to_scenario(_edge()), _cloud())
