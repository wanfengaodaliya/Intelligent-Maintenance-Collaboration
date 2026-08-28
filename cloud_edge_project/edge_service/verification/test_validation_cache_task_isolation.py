from __future__ import annotations

from edge_validation_cache import (
    EdgeValidationCache,
    ValidationCacheConfig,
    ValidationCacheInvocationContext,
)


def _packet(*, device_id: str, task_id: str, packet_id: str) -> dict:
    def channel(sample_rate_hz: int, sample_count: int, unit: str) -> dict:
        return {
            "sample_rate_hz": sample_rate_hz,
            "sample_count": sample_count,
            "unit": unit,
            "values": [0.0] * sample_count,
        }

    return {
        "device_id": device_id,
        "bearing_id": "bearing_01",
        "task_id": task_id,
        "packet_id": packet_id,
        "sender_id": "sender_01",
        "sequence_number": 1,
        "end_generate_timestamp_ns": 1_000_000_000,
        "data": {
            "vibration": channel(64_000, 3_200, "mm/s"),
            "phase_current_1_A": channel(64_000, 3_200, "A"),
            "phase_current_2_A": channel(64_000, 3_200, "A"),
            "shaft_speed_rpm": channel(4_000, 200, "rpm"),
            "load_torque_nm": channel(4_000, 200, "N*m"),
            "bearing_radial_load_n": channel(4_000, 200, "N"),
            "bearing_module_temperature_c": 40.0,
        },
    }


def test_same_sender_accepts_a_new_device_before_old_cache_expires() -> None:
    cache = EdgeValidationCache(
        ValidationCacheConfig(
            raw_cache_retention_seconds=60,
            max_receive_rate_per_sender=20,
            context_queue_capacity_per_sender=1_200,
            raw_cache_capacity_per_sender=1_200,
            context_before_packet_count=20,
            cache_cleanup_interval_seconds=1,
            hard_value_ranges={},
        ),
        clock_ns=lambda: 2_000_000_000,
    )
    first = _packet(
        device_id="machine_01", task_id="sd_01_tk_0001", packet_id="packet_01"
    )
    second = _packet(
        device_id="machine_02", task_id="sd_01_tk_0002", packet_id="packet_02"
    )

    first_result = cache.process(
        first,
        ValidationCacheInvocationContext(1_000_000_000),
        ("sender_01", "sd_01_tk_0001", 1),
    )
    second_result = cache.process(
        second,
        ValidationCacheInvocationContext(1_100_000_000),
        ("sender_01", "sd_01_tk_0002", 1),
    )

    assert first_result.status.success is True
    assert second_result.status.success is True
    assert cache.read(("sender_01", "sd_01_tk_0001", 1)) is not None
    assert cache.read(("sender_01", "sd_01_tk_0002", 1)) is not None
