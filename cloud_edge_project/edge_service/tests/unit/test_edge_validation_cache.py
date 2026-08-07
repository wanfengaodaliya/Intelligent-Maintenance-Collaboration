# -*- coding: utf-8 -*-
"""严格校验、上下文队列和原始环形缓存单元测试。"""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from edge_validation_cache import (
    EdgeValidationCache,
    ValidationCacheConfig,
    ValidationCacheInvocationContext,
    ValueRange,
)


_BASE_NS = 1_800_000_000_000_000_000


class _Clock:
    def __init__(self, value: int = _BASE_NS):
        self.value = value

    def __call__(self) -> int:
        return self.value


def _config(**overrides) -> ValidationCacheConfig:
    values = {
        "raw_cache_retention_seconds": 60.0,
        "max_receive_rate_per_sender": 20.0,
        "context_queue_capacity_per_sender": 1201,
        "raw_cache_capacity_per_sender": 1201,
        "context_before_packet_count": 20,
        "cache_cleanup_interval_seconds": 0.01,
        "hard_value_ranges": {},
    }
    values.update(overrides)
    return ValidationCacheConfig(**values)


def _channel(count: int, sample_rate_hz: int, value: float, unit: str | None = None):
    result = {
        "sample_rate_hz": sample_rate_hz,
        "sample_count": count,
        "values": [value] * count,
    }
    if unit is not None:
        result["unit"] = unit
    return result


def _packet(
    *,
    sender: str = "sender-1",
    bearing: str = "bearing-1",
    task: str = "task-1",
    packet_id: str = "packet-1",
    sequence: int = 1,
    timestamp: int = 1_700_000_000_000_000_000,
):
    return {
        "device_id": "device-1",
        "bearing_id": bearing,
        "sender_id": sender,
        "task_id": task,
        "packet_id": packet_id,
        "sequence_number": sequence,
        "end_generate_timestamp_ns": timestamp,
        "data": {
            "vibration": _channel(3200, 64000, 1.0, "mm/s"),
            "phase_current_1_A": _channel(3200, 64000, 2.0, "A"),
            "phase_current_2_A": _channel(3200, 64000, 3.0, "A"),
            "shaft_speed_rpm": _channel(200, 4000, 1500.0),
            "load_torque_nm": _channel(200, 4000, 10.0),
            "bearing_radial_load_n": _channel(200, 4000, 1000.0),
            "bearing_module_temperature_c": 46.3,
        },
    }


def _ref(packet) -> tuple[str, str, int]:
    return packet["sender_id"], packet["task_id"], packet["sequence_number"]


def _context(index: int = 0) -> ValidationCacheInvocationContext:
    return ValidationCacheInvocationContext(_BASE_NS + index * 50_000_000)


def _request(packet, **overrides):
    request = {
        "request_id": "context-request-1",
        "device_id": packet["device_id"],
        "bearing_id": packet["bearing_id"],
        "sender_id": packet["sender_id"],
        "anchor_packet_id": packet["packet_id"],
        "anchor_end_generate_timestamp_ns": packet["end_generate_timestamp_ns"],
        "before_packet_count": 20,
        "requested_at_ns": _BASE_NS + 2_000_000_000,
    }
    request.update(overrides)
    return request


def test_valid_packet_is_cached_atomically_and_computation_copy_is_independent():
    clock = _Clock()
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    raw = _packet()
    before = copy.deepcopy(raw)

    result = cache.process(raw, _context(), _ref(raw))

    assert result.status.success
    assert raw == before
    assert result.payload["data"]["vibration"]["values"].flags.writeable
    cached = cache.read(_ref(raw))
    assert cached is not None
    assert not cached["data"]["vibration"]["values"].flags.writeable
    assert np.all(cached["data"]["vibration"]["values"] == 1.0)
    result.payload["data"]["vibration"]["values"][0] = 99.0
    assert cache.read(_ref(raw))["data"]["vibration"]["values"][0] == 1.0
    slots = cache.context_snapshot("sender-1")
    assert len(slots) == 1
    assert slots[0].received_at_ns == _BASE_NS
    assert slots[0].cache_status == "AVAILABLE"
    assert slots[0].raw_packet_ref == _ref(raw)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda packet: packet.pop("data"), "REQUIRED_FIELD_MISSING"),
        (lambda packet: packet["data"].pop("vibration"), "REQUIRED_FIELD_MISSING"),
        (lambda packet: packet["data"].__setitem__("vibration", 1), "INVALID_FIELD_TYPE"),
        (
            lambda packet: packet["data"]["vibration"].__setitem__("values", "bad"),
            "INVALID_FIELD_TYPE",
        ),
        (
            lambda packet: packet["data"]["vibration"].__setitem__("unit", "g"),
            "INVALID_CHANNEL_UNIT",
        ),
        (
            lambda packet: packet["data"]["vibration"].__setitem__(
                "sample_rate_hz", 32000
            ),
            "INVALID_SAMPLE_RATE",
        ),
        (
            lambda packet: packet["data"]["vibration"].__setitem__("sample_count", 100),
            "INVALID_SAMPLE_COUNT",
        ),
        (
            lambda packet: packet["data"]["vibration"].__setitem__("values", []),
            "EMPTY_CHANNEL_VALUES",
        ),
        (
            lambda packet: packet["data"]["vibration"].__setitem__(
                "values", [1.0] * 3199
            ),
            "SAMPLE_COUNT_MISMATCH",
        ),
        (
            lambda packet: packet["data"]["vibration"]["values"].__setitem__(0, np.nan),
            "NON_FINITE_VALUE",
        ),
        (
            lambda packet: packet["data"]["vibration"]["values"].__setitem__(0, 1 + 2j),
            "INVALID_FIELD_TYPE",
        ),
    ],
)
def test_validation_errors_are_stable_and_do_not_write_raw_cache(mutate, expected):
    logs = []
    cache = EdgeValidationCache(_config(), on_error=logs.append)
    raw = _packet()
    mutate(raw)

    result = cache.process(raw, _context(), _ref(raw))

    assert not result.status.success
    assert result.status.error_code == expected
    assert result.payload is None
    assert cache.read(_ref(raw)) is None
    assert cache.context_snapshot("sender-1")[0].cache_status == "VALIDATION_REJECTED"
    assert logs[-1]["error_code"] == expected
    assert "values" not in logs[-1]


def test_invalid_timestamp_creates_rejected_context_position():
    cache = EdgeValidationCache(_config())
    raw = _packet(timestamp=0)

    result = cache.process(raw, _context(), _ref(raw))

    assert result.status.error_code == "INVALID_TIMESTAMP"
    slot = cache.context_snapshot("sender-1")[0]
    assert slot.end_generate_timestamp_ns is None
    assert slot.cache_status == "VALIDATION_REJECTED"
    assert slot.raw_packet_ref is None


def test_configured_hard_range_is_enforced_and_missing_ranges_are_not_invented():
    ranged = EdgeValidationCache(
        _config(hard_value_ranges={"vibration": ValueRange(-0.5, 0.5)})
    )
    raw = _packet()
    assert ranged.process(raw, _context(), _ref(raw)).status.error_code == "VALUE_OUT_OF_RANGE"

    unrestricted = EdgeValidationCache(_config(hard_value_ranges={}))
    raw = _packet()
    raw["data"]["vibration"]["values"] = [1e100] * 3200
    assert unrestricted.process(raw, _context(), _ref(raw)).status.success


def test_duplicate_raw_ref_never_overwrites_original_packet():
    cache = EdgeValidationCache(_config())
    first = _packet(packet_id="first")
    second = _packet(packet_id="second")
    assert cache.process(first, _context(), _ref(first)).status.success

    duplicate = cache.process(second, _context(1), _ref(second))

    assert duplicate.status.error_code == "RAW_CACHE_WRITE_FAILED"
    assert cache.read(_ref(first))["packet_id"] == "first"
    assert [slot.cache_status for slot in cache.context_snapshot("sender-1")] == [
        "AVAILABLE",
        "CACHE_WRITE_FAILED",
    ]


def test_raw_cache_capacity_failure_does_not_evict_unexpired_packet():
    config = _config(
        max_receive_rate_per_sender=1.0 / 60.0,
        context_queue_capacity_per_sender=2,
        raw_cache_capacity_per_sender=1,
    )
    cache = EdgeValidationCache(config)
    first = _packet(packet_id="first", sequence=1)
    second = _packet(packet_id="second", sequence=2)
    assert cache.process(first, _context(), _ref(first)).status.success

    result = cache.process(second, _context(1), _ref(second))

    assert result.status.error_code == "RAW_CACHE_WRITE_FAILED"
    assert cache.read(_ref(first)) is not None
    assert cache.read(_ref(second)) is None
    assert cache.context_snapshot("sender-1")[-1].cache_status == "CACHE_WRITE_FAILED"


def test_context_queue_capacity_never_auto_overwrites_unexpired_slot():
    config = _config(
        max_receive_rate_per_sender=1.0 / 60.0,
        context_queue_capacity_per_sender=1,
        raw_cache_capacity_per_sender=2,
    )
    cache = EdgeValidationCache(config)
    first = _packet(packet_id="first", sequence=1, timestamp=0)
    second = _packet(packet_id="second", sequence=2)
    assert cache.process(first, _context(), _ref(first)).status.error_code == "INVALID_TIMESTAMP"

    result = cache.process(second, _context(1), _ref(second))

    assert result.status.error_code == "RAW_CACHE_WRITE_FAILED"
    slots = cache.context_snapshot("sender-1")
    assert len(slots) == 1
    assert slots[0].packet_id == "first"


def test_same_sequence_from_different_senders_is_isolated():
    cache = EdgeValidationCache(_config())
    first = _packet(sender="sender-a", bearing="bearing-a", packet_id="packet-a")
    second = _packet(sender="sender-b", bearing="bearing-b", packet_id="packet-b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: cache.process(item, _context(), _ref(item)),
                (first, second),
            )
        )

    assert all(result.status.success for result in results)
    assert cache.read(_ref(first))["bearing_id"] == "bearing-a"
    assert cache.read(_ref(second))["bearing_id"] == "bearing-b"


def test_sender_binding_cannot_change_until_old_context_expires():
    cache = EdgeValidationCache(_config())
    first = _packet(sender="sender-1", bearing="bearing-a", packet_id="first")
    rebound = _packet(
        sender="sender-1",
        bearing="bearing-b",
        task="task-2",
        packet_id="second",
    )
    assert cache.process(first, _context(), _ref(first)).status.success
    assert (
        cache.process(rebound, _context(1), _ref(rebound)).status.error_code
        == "RAW_CACHE_WRITE_FAILED"
    )

    after_retention = _BASE_NS + 60_000_000_000
    result = cache.process(
        rebound,
        ValidationCacheInvocationContext(after_retention),
        _ref(rebound),
    )
    assert result.status.success
    assert cache.context_snapshot("sender-1")[0].bearing_id == "bearing-b"


def test_complete_context_query_returns_previous_twenty_across_task_boundary():
    clock = _Clock(_BASE_NS + 2_000_000_000)
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    packets = []
    for index in range(21):
        task = "task-a" if index < 10 else "task-b"
        sequence = index + 1 if index < 10 else index - 9
        packet = _packet(
            task=task,
            packet_id=f"packet-{index + 1}",
            sequence=sequence,
            timestamp=1_700_000_000_000_000_000 + index * 50_000_000,
        )
        packets.append(packet)
        assert cache.process(packet, _context(index), _ref(packet)).status.success

    result = cache.query_context(_request(packets[-1]))

    assert result["context_status"] == "COMPLETE"
    assert result["available_count"] == 20
    assert result["unavailable_packets"] == []
    assert result["packets"][0]["packet_id"] == "packet-1"
    assert result["packets"][-1]["packet_id"] == "packet-20"
    assert {packet["task_id"] for packet in result["packets"]} == {"task-a", "task-b"}
    assert all(not packet["data"]["vibration"]["values"].flags.writeable for packet in result["packets"])


def test_context_query_reports_permanent_gap_without_using_older_replacement():
    clock = _Clock(_BASE_NS + 2_000_000_000)
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    packets = []
    for index in range(21):
        packet = _packet(
            packet_id=f"packet-{index + 1}",
            sequence=index + 1,
            timestamp=1_700_000_000_000_000_000 + index * 50_000_000,
        )
        packets.append(packet)
        if index == 4:
            packet["data"].pop("vibration")
        cache.process(packet, _context(index), _ref(packet))

    result = cache.query_context(_request(packets[-1]))

    assert result["context_status"] == "INSUFFICIENT_CONTEXT"
    assert result["available_count"] == 19
    assert result["unavailable_packets"][0]["packet_id"] == "packet-5"
    assert result["unavailable_packets"][0]["cache_status"] == "VALIDATION_REJECTED"


def test_context_query_reports_pending_slot_during_concurrent_validation():
    clock = _Clock(_BASE_NS + 2_000_000_000)
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    entered = threading.Event()
    release = threading.Event()
    original_validate = cache._validate_data

    def blocking_validate(packet, identity, timestamp):
        if packet["packet_id"] == "packet-5":
            entered.set()
            assert release.wait(timeout=2.0)
        return original_validate(packet, identity, timestamp)

    cache._validate_data = blocking_validate
    packets = []
    blocked_result = []
    blocked_thread = None
    try:
        for index in range(21):
            packet = _packet(
                packet_id=f"packet-{index + 1}",
                sequence=index + 1,
                timestamp=1_700_000_000_000_000_000 + index * 50_000_000,
            )
            packets.append(packet)
            if index == 4:
                blocked_thread = threading.Thread(
                    target=lambda: blocked_result.append(
                        cache.process(packet, _context(index), _ref(packet))
                    )
                )
                blocked_thread.start()
                assert entered.wait(timeout=2.0)
            else:
                assert cache.process(packet, _context(index), _ref(packet)).status.success

        result = cache.query_context(_request(packets[-1]))
        assert result["context_status"] == "PENDING_CONTEXT"
        assert result["available_count"] == 19
        assert result["unavailable_packets"][0]["packet_id"] == "packet-5"
        assert result["unavailable_packets"][0]["cache_status"] == "PENDING"
    finally:
        release.set()
        if blocked_thread is not None:
            blocked_thread.join(timeout=2.0)
    assert blocked_result and blocked_result[0].status.success


def test_context_query_distinguishes_shortage_invalid_request_and_missing_anchor():
    clock = _Clock(_BASE_NS + 1_000_000_000)
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    packets = []
    for index in range(5):
        packet = _packet(
            packet_id=f"packet-{index + 1}",
            sequence=index + 1,
            timestamp=1_700_000_000_000_000_000 + index,
        )
        packets.append(packet)
        cache.process(packet, _context(index), _ref(packet))

    shortage = cache.query_context(_request(packets[-1]))
    assert shortage["context_status"] == "INSUFFICIENT_CONTEXT"
    assert shortage["available_count"] == 4
    assert len(shortage["unavailable_packets"]) == 16

    invalid = cache.query_context(_request(packets[-1], before_packet_count=10))
    assert invalid["context_status"] == "INVALID_CONTEXT_REQUEST"
    missing = cache.query_context(_request(packets[-1], anchor_packet_id="not-found"))
    assert missing["context_status"] == "ANCHOR_NOT_FOUND"


def test_duplicate_anchor_identity_is_reported_as_not_unique():
    clock = _Clock(_BASE_NS + 1_000_000_000)
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    first = _packet(task="task-a", packet_id="same", sequence=1)
    second = _packet(task="task-b", packet_id="same", sequence=1)
    assert cache.process(first, _context(), _ref(first)).status.success
    assert cache.process(second, _context(1), _ref(second)).status.success

    result = cache.query_context(_request(second))

    assert result["context_status"] == "ANCHOR_NOT_UNIQUE"


def test_cleanup_respects_retention_boundary_and_removes_slot_and_packet_together():
    clock = _Clock()
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    packet = _packet()
    assert cache.process(packet, _context(), _ref(packet)).status.success

    assert cache.cleanup_expired(_BASE_NS + 60_000_000_000 - 1) == 0
    assert cache.context_snapshot("sender-1")
    assert cache.cleanup_expired(_BASE_NS + 60_000_000_000) == 1
    assert cache.context_snapshot("sender-1") == ()
    clock.value = _BASE_NS + 60_000_000_000
    assert cache.read(_ref(packet)) is None


def test_background_cleanup_runs_without_new_packets():
    clock = _Clock()
    cache = EdgeValidationCache(_config(), clock_ns=clock)
    packet = _packet()
    cache.process(packet, _context(), _ref(packet))
    clock.value = _BASE_NS + 61_000_000_000

    cache.start()
    try:
        deadline = time.monotonic() + 1.0
        while cache.context_snapshot("sender-1") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cache.context_snapshot("sender-1") == ()
    finally:
        cache.stop()


def test_parallel_senders_do_not_cross_slots_or_cached_identity():
    cache = EdgeValidationCache(_config())

    def process(index: int):
        packet = _packet(
            sender=f"sender-{index}",
            bearing=f"bearing-{index}",
            task="shared-task",
            packet_id=f"packet-{index}",
        )
        result = cache.process(packet, _context(), _ref(packet))
        return packet, result

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(process, range(18)))

    assert all(result.status.success for _, result in results)
    for packet, _ in results:
        cached = cache.read(_ref(packet))
        assert cached["sender_id"] == packet["sender_id"]
        assert cached["bearing_id"] == packet["bearing_id"]
        slots = cache.context_snapshot(packet["sender_id"])
        assert len(slots) == 1
        assert slots[0].bearing_id == packet["bearing_id"]


def test_invalid_config_is_rejected_before_processing_starts():
    with pytest.raises(ValueError, match="context_queue_capacity_per_sender"):
        EdgeValidationCache(_config(context_queue_capacity_per_sender=1199))
    with pytest.raises(ValueError, match="context_before_packet_count"):
        EdgeValidationCache(_config(context_before_packet_count=10))
