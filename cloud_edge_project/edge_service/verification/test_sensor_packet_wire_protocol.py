from __future__ import annotations

import json

import pytest

from edge_runtime.mqtt import decode_sensor_packet
from sender_module.sender.packet import (
    PacketValidationError,
    build_sensor_packet,
    serialize_packet,
)


def _signals() -> dict:
    signals = {
        name: {
            "sample_rate_hz": sample_rate_hz,
            "sample_count": 3,
            "values": [0.0, 1.25, -2.5],
        }
        for name, sample_rate_hz in {
            "vibration": 64000,
            "phase_current_1_A": 64000,
            "phase_current_2_A": 64000,
            "shaft_speed_rpm": 4000,
            "load_torque_nm": 4000,
            "bearing_radial_load_n": 4000,
        }.items()
    }
    signals["vibration"]["unit"] = "mm/s"
    signals["phase_current_1_A"]["unit"] = "A"
    signals["phase_current_2_A"]["unit"] = "A"
    signals["bearing_module_temperature_c"] = 25.0
    return signals


def _packet() -> dict:
    return build_sensor_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=_signals(),
        end_generate_timestamp_ns=1,
    )


def test_sender_binary_packet_round_trips_through_edge_decoder() -> None:
    packet = _packet()
    payload = serialize_packet(packet)

    decoded = decode_sensor_packet(payload)

    assert payload.startswith(b"IMC1")
    assert decoded["packet_id"] == packet["packet_id"]
    for signal_name, signal in packet["data"].items():
        if signal_name == "bearing_module_temperature_c":
            assert decoded["data"][signal_name] == signal
        else:
            assert decoded["data"][signal_name]["values"] == pytest.approx(
                signal["values"]
            )


def test_edge_decoder_keeps_legacy_json_compatibility() -> None:
    packet = {"packet_id": "legacy_packet", "data": {"legacy": True}}

    assert decode_sensor_packet(json.dumps(packet).encode("utf-8")) == packet


def test_edge_decoder_rejects_truncated_binary_body() -> None:
    with pytest.raises(ValueError, match="range|unused|missing"):
        decode_sensor_packet(serialize_packet(_packet())[:-4])


def test_sender_rejects_non_finite_float32_values() -> None:
    packet = _packet()
    packet["data"]["vibration"]["values"][0] = float("nan")

    with pytest.raises(PacketValidationError, match="finite float32"):
        serialize_packet(packet)
