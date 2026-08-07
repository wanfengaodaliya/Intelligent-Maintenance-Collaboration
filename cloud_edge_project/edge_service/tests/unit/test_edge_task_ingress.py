# -*- coding: utf-8 -*-
"""任务注册、原始包匹配和校验缓存编排测试。"""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from edge_task_ingress import (
    INGRESS_ACCEPTED,
    INGRESS_DUPLICATE,
    INGRESS_REJECTED,
    INPUT_REFERENCE_MISMATCH,
    INVALID_SEQUENCE_NUMBER,
    INVALID_TASK,
    PACKET_CONTENT_CONFLICT,
    PACKET_NOT_RECEIVED,
    TARGET_NODE_MISMATCH,
    TASK_CONFLICT,
    TASK_NOT_FOUND,
    TASK_SEQUENCE_CONFLICT,
    UNSUPPORTED_TASK_TYPE,
    EdgeTaskIngress,
    TaskIngressConfig,
)
from edge_validation_cache import (
    EdgeValidationCache,
    ModuleResult,
    ValidationCacheConfig,
)
from edge_validation_cache.contracts import RAW_CACHE_WRITE_FAILED


_BASE_NS = 1_800_000_000_000_000_000


class _Clock:
    def __init__(self):
        self._value = _BASE_NS
        self._mutex = threading.Lock()

    def __call__(self) -> int:
        with self._mutex:
            value = self._value
            self._value += 1
            return value


class _FakeValidationCache:
    def __init__(self, error_code: str | None = None):
        self.error_code = error_code
        self.calls = []
        self._mutex = threading.Lock()

    def process(self, raw_packet, context, raw_packet_ref):
        with self._mutex:
            self.calls.append((raw_packet, context, raw_packet_ref))
        if self.error_code is not None:
            return ModuleResult.failed(self.error_code)
        return ModuleResult.succeeded({"validated": raw_packet["packet_id"]})


def _dispatch(**overrides):
    result = {
        "task_id": "task-1",
        "target_edge_node_id": "edge-1",
        "task_type": "BEARING_EDGE_INFERENCE",
        "input_ref": {
            "device_id": "device-1",
            "expected_bearing_ids": ["bearing-1", "bearing-2"],
            "assigned_bearings": [
                {
                    "bearing_id": "bearing-1",
                    "sender_id": "sender-1",
                    "expected_packet_count": 80,
                },
                {
                    "bearing_id": "bearing-2",
                    "sender_id": "sender-2",
                    "expected_packet_count": 80,
                },
            ],
        },
        "dispatched_at_ns": 1_700_000_000_000_000_000,
    }
    result.update(overrides)
    return result


def _packet(
    *,
    bearing: str = "bearing-1",
    sender: str = "sender-1",
    packet_id: str = "packet-1",
    sequence: int = 1,
    task: str = "task-1",
):
    return {
        "device_id": "device-1",
        "bearing_id": bearing,
        "sender_id": sender,
        "task_id": task,
        "packet_id": packet_id,
        "sequence_number": sequence,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000 + sequence,
        "data": {"test_value": 1.0},
    }


def _ingress(cache=None, clock=None):
    return EdgeTaskIngress(
        TaskIngressConfig(edge_node_id="edge-1"),
        cache or _FakeValidationCache(),
        clock_ns=clock or _Clock(),
    )


def _register(ingress):
    ack = ingress.register_task(_dispatch())
    assert ack.ack_status == "ACCEPTED"
    return ack


def test_task_is_registered_atomically_and_exact_duplicate_is_idempotent():
    ingress = _ingress()
    first = ingress.register_task(_dispatch())
    reordered = _dispatch()
    reordered["input_ref"]["expected_bearing_ids"].reverse()
    reordered["input_ref"]["assigned_bearings"].reverse()

    duplicate = ingress.register_task(reordered)
    snapshot = ingress.task_snapshot("task-1")

    assert first.ack_status == "ACCEPTED"
    assert duplicate == first
    assert snapshot.task_status == "WAITING_FOR_INPUT"
    assert set(snapshot.bearing_task_records) == {"bearing-1", "bearing-2"}
    assert snapshot.bearing_task_records["bearing-1"].sender_id == "sender-1"


def test_invalid_wrong_target_unsupported_and_conflicting_tasks_are_rejected():
    ingress = _ingress()
    wrong_target = ingress.register_task(
        _dispatch(task_id="wrong-target", target_edge_node_id="edge-2")
    )
    unsupported = ingress.register_task(
        _dispatch(task_id="unsupported", task_type="OTHER")
    )
    malformed = _dispatch(task_id="malformed")
    del malformed["dispatched_at_ns"]
    invalid = ingress.register_task(malformed)
    _register(ingress)
    changed = _dispatch()
    changed["input_ref"]["assigned_bearings"][0]["sender_id"] = "changed"
    conflict = ingress.register_task(changed)

    assert wrong_target.reason_code == TARGET_NODE_MISMATCH
    assert unsupported.reason_code == UNSUPPORTED_TASK_TYPE
    assert invalid.reason_code == INVALID_TASK
    assert conflict.reason_code == TASK_CONFLICT
    assert ingress.task_snapshot("wrong-target") is None
    assert ingress.task_snapshot("unsupported") is None
    assert ingress.task_snapshot("malformed") is None
    assert ingress.task_snapshot("task-1").bearing_task_records["bearing-1"].sender_id == "sender-1"


def test_packet_before_task_is_rejected_without_cache_or_record():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)

    result = ingress.receive_packet(_packet())

    assert result.status == INGRESS_REJECTED
    assert result.error_code == TASK_NOT_FOUND
    assert result.packet_record is None
    assert cache.calls == []
    assert ingress.task_snapshot("task-1") is None


def test_valid_packet_uses_one_received_time_and_advances_to_downsampling():
    clock = _Clock()
    cache = _FakeValidationCache()
    ingress = _ingress(cache, clock)
    _register(ingress)

    result = ingress.receive_packet(_packet())

    assert result.status == INGRESS_ACCEPTED
    assert result.error_code is None
    assert result.packet_record.packet_status == "PROCESSING"
    assert result.packet_record.current_stage == "DOWNSAMPLING"
    assert result.packet_record.raw_packet_ref == ("sender-1", "task-1", 1)
    assert result.validated_packet == {"validated": "packet-1"}
    assert cache.calls[0][1].received_at_ns == result.received_at_ns
    assert cache.calls[0][2] == ("sender-1", "task-1", 1)
    snapshot = ingress.task_snapshot("task-1")
    assert snapshot.started_at_ns == result.received_at_ns
    assert snapshot.bearing_task_records["bearing-1"].received_packet_count == 1


def test_duplicate_and_conflicts_do_not_call_validation_cache_again():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)
    _register(ingress)
    original = _packet()
    first = ingress.receive_packet(original)
    duplicate = ingress.receive_packet(copy.deepcopy(original))
    changed_content = copy.deepcopy(original)
    changed_content["data"]["test_value"] = 2.0
    content_conflict = ingress.receive_packet(changed_content)
    sequence_conflict = ingress.receive_packet(_packet(packet_id="another"))

    assert first.status == INGRESS_ACCEPTED
    assert duplicate.status == INGRESS_DUPLICATE
    assert duplicate.packet_record.received_at_ns == first.received_at_ns
    assert content_conflict.error_code == PACKET_CONTENT_CONFLICT
    assert sequence_conflict.error_code == TASK_SEQUENCE_CONFLICT
    assert len(cache.calls) == 1
    bearing = ingress.task_snapshot("task-1").bearing_task_records["bearing-1"]
    assert bearing.received_packet_count == 1


def test_same_packet_id_at_different_sequence_conflicts_only_within_bearing():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)
    _register(ingress)
    ingress.receive_packet(_packet(packet_id="shared", sequence=1))

    same_bearing = ingress.receive_packet(_packet(packet_id="shared", sequence=2))
    other_bearing = ingress.receive_packet(
        _packet(
            bearing="bearing-2",
            sender="sender-2",
            packet_id="shared",
            sequence=1,
        )
    )

    assert same_bearing.error_code == PACKET_CONTENT_CONFLICT
    assert other_bearing.status == INGRESS_ACCEPTED
    assert len(cache.calls) == 2


def test_packet_identity_binding_and_sequence_range_are_enforced():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)
    _register(ingress)

    wrong_sender = ingress.receive_packet(_packet(sender="sender-2"))
    out_of_range = ingress.receive_packet(_packet(sequence=81))
    missing_identity = _packet()
    del missing_identity["bearing_id"]
    malformed = ingress.receive_packet(missing_identity)

    assert wrong_sender.error_code == INPUT_REFERENCE_MISMATCH
    assert out_of_range.error_code == INVALID_SEQUENCE_NUMBER
    assert malformed.error_code == INPUT_REFERENCE_MISMATCH
    assert cache.calls == []


def test_validation_and_cache_failures_update_packet_terminal_counters():
    validation_cache = _FakeValidationCache("INVALID_SAMPLE_COUNT")
    validation_ingress = _ingress(validation_cache)
    _register(validation_ingress)
    rejected = validation_ingress.receive_packet(_packet())
    bearing = validation_ingress.task_snapshot("task-1").bearing_task_records["bearing-1"]

    assert rejected.status == INGRESS_ACCEPTED
    assert rejected.error_code == "INVALID_SAMPLE_COUNT"
    assert rejected.packet_record.packet_status == "VALIDATION_REJECTED"
    assert rejected.packet_record.current_stage is None
    assert rejected.packet_record.raw_packet_ref is None
    assert not rejected.packet_record.summary_generated
    assert bearing.validation_rejected_count == 1
    assert bearing.terminal_packet_count == 1

    failing_cache = _FakeValidationCache(RAW_CACHE_WRITE_FAILED)
    failing_ingress = _ingress(failing_cache)
    _register(failing_ingress)
    failed = failing_ingress.receive_packet(_packet())
    failed_bearing = failing_ingress.task_snapshot("task-1").bearing_task_records["bearing-1"]
    assert failed.packet_record.packet_status == "PROCESSING_FAILED"
    assert failed_bearing.processing_failed_count == 1
    assert failed_bearing.terminal_packet_count == 1


def test_sequence_80_creates_missing_terminal_records_without_summaries():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)
    _register(ingress)
    ingress.receive_packet(_packet(packet_id="packet-1", sequence=1))

    result = ingress.receive_packet(_packet(packet_id="packet-80", sequence=80))
    bearing = ingress.task_snapshot("task-1").bearing_task_records["bearing-1"]

    assert result.status == INGRESS_ACCEPTED
    assert bearing.end_sequence_received
    assert bearing.data_completeness == "INCOMPLETE"
    assert bearing.missing_sequence_numbers == tuple(range(2, 80))
    assert bearing.missing_packet_count == 78
    assert bearing.terminal_packet_count == 78
    assert bearing.summary_generated_count == 0
    missing = bearing.packet_records[2]
    assert missing.packet_id is None
    assert missing.packet_status == "NOT_RECEIVED"
    assert missing.error_code == PACKET_NOT_RECEIVED
    assert not missing.summary_generated
    assert len(cache.calls) == 2


def test_concurrent_bearings_may_reuse_packet_and_sequence_identity():
    cache = _FakeValidationCache()
    ingress = _ingress(cache)
    _register(ingress)
    packets = [
        _packet(bearing="bearing-1", sender="sender-1", packet_id="same", sequence=1),
        _packet(bearing="bearing-2", sender="sender-2", packet_id="same", sequence=1),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingress.receive_packet, packets))

    assert all(result.status == INGRESS_ACCEPTED for result in results)
    snapshot = ingress.task_snapshot("task-1")
    assert snapshot.bearing_task_records["bearing-1"].received_packet_count == 1
    assert snapshot.bearing_task_records["bearing-2"].received_packet_count == 1
    assert len(cache.calls) == 2


def test_real_validation_cache_receives_the_ingress_timestamp_and_stores_packet():
    clock = _Clock()
    cache = EdgeValidationCache(
        ValidationCacheConfig(
            raw_cache_retention_seconds=60,
            max_receive_rate_per_sender=20,
            context_queue_capacity_per_sender=1200,
            raw_cache_capacity_per_sender=1200,
            context_before_packet_count=20,
            cache_cleanup_interval_seconds=1,
            hard_value_ranges={},
        ),
        clock_ns=clock,
    )
    ingress = _ingress(cache, clock)
    _register(ingress)
    packet = _full_packet()

    result = ingress.receive_packet(packet)
    slots = cache.context_snapshot("sender-1")

    assert result.status == INGRESS_ACCEPTED
    assert result.error_code is None
    assert slots[0].received_at_ns == result.received_at_ns
    assert slots[0].raw_packet_ref == ("sender-1", "task-1", 1)
    assert cache.read(slots[0].raw_packet_ref) is not None


def _full_packet():
    packet = _packet()

    def channel(count, rate, value, unit=None):
        result = {
            "sample_rate_hz": rate,
            "sample_count": count,
            "values": [value] * count,
        }
        if unit is not None:
            result["unit"] = unit
        return result

    packet["data"] = {
        "vibration": channel(3200, 64000, 1.0, "mm/s"),
        "phase_current_1_A": channel(3200, 64000, 2.0, "A"),
        "phase_current_2_A": channel(3200, 64000, 3.0, "A"),
        "shaft_speed_rpm": channel(200, 4000, 1500.0),
        "load_torque_nm": channel(200, 4000, 10.0),
        "bearing_radial_load_n": channel(200, 4000, 1000.0),
        "bearing_module_temperature_c": 46.3,
    }
    return packet
