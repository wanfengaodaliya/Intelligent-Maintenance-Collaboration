from __future__ import annotations

import pytest

from compatibility.bearing_v12.scheduler_mapper import (
    SchedulerMappingError,
    assignment_to_domain,
    assignment_to_legacy,
    capability_to_domain,
    capability_to_legacy,
    device_payload_to_legacy,
    device_request_to_domain,
    packet_result_to_domain,
    packet_result_to_legacy,
)
from scheduler.assignment_scheduler import validate_assignment_request


def test_assignment_mapper_accepts_legacy_generic_and_matching_aliases() -> None:
    legacy = {"task_id": "task_1", "bearing_id": "bearing_01"}
    generic = {"task_id": "task_1", "unit_id": "bearing_01"}
    matching = {**legacy, "unit_id": "bearing_01"}

    assert assignment_to_domain(legacy) == generic
    assert assignment_to_domain(generic) == generic
    assert assignment_to_domain(matching) == generic
    assert assignment_to_legacy(generic) == legacy


def test_assignment_mapper_rejects_conflicting_aliases() -> None:
    with pytest.raises(SchedulerMappingError, match="unit_id and bearing_id"):
        assignment_to_domain(
            {"unit_id": "bearing_01", "bearing_id": "bearing_02"}
        )


def test_packet_mapper_converts_top_level_and_input_reference() -> None:
    legacy = {
        "bearing_id": "bearing_01",
        "input_ref": {"bearing_id": "bearing_01", "packet_id": "packet_1"},
    }

    domain = packet_result_to_domain(legacy)

    assert domain["unit_id"] == "bearing_01"
    assert domain["input_ref"]["unit_id"] == "bearing_01"
    assert packet_result_to_legacy(domain) == legacy


def test_device_mapper_preserves_result_order_and_round_trips() -> None:
    legacy = {
        "expected_bearing_count": 2,
        "received_bearing_count": 2,
        "bearing_result_ids": ["result_1", "result_2"],
        "bearing_results": [
            {"bearing_id": "bearing_01", "bearing_result_id": "result_1"},
            {"bearing_id": "bearing_02", "bearing_result_id": "result_2"},
        ],
        "comparison": {
            "low_confidence_bearing_count": 1,
            "provisional_bearing_count": 0,
        },
    }

    domain = device_request_to_domain(legacy)

    assert domain["expected_unit_count"] == 2
    assert domain["received_unit_count"] == 2
    assert domain["unit_result_ids"] == ["result_1", "result_2"]
    assert [item["unit_id"] for item in domain["unit_results"]] == [
        "bearing_01",
        "bearing_02",
    ]
    assert domain["comparison"]["low_confidence_unit_count"] == 1
    assert device_payload_to_legacy(domain) == legacy


def test_capability_mapper_preserves_legacy_edge_contract() -> None:
    assert capability_to_domain("BEARING_EDGE_INFERENCE") == "edge_inference"
    assert capability_to_legacy("edge_inference") == "BEARING_EDGE_INFERENCE"


def test_assignment_validation_is_identical_for_legacy_and_generic_input() -> None:
    legacy = {
        "device_id": "machine_01",
        "sender_id": "sender_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_size_bytes": 100_000,
        "expected_packet_count": 80,
        "expected_duration_ms": 4_000,
        "created_timestamp_ns": 1,
    }
    generic = {**legacy, "unit_id": legacy["bearing_id"]}
    generic.pop("bearing_id")

    assert validate_assignment_request(legacy) == validate_assignment_request(generic)
