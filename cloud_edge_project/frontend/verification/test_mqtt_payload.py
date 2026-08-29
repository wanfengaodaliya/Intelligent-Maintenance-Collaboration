from __future__ import annotations

import json

import pytest

from frontend.dashboard_state import DashboardSession
from frontend.mqtt_payload import decode_dashboard_payload
from sender_module.sender.packet import build_sensor_packet, serialize_packet


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


def _binary_packet() -> bytes:
    packet = build_sensor_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=_signals(),
        end_generate_timestamp_ns=1,
        run_id="run_01",
    )
    return serialize_packet(packet)


def test_binary_packet_exposes_dashboard_metadata_without_signal_arrays() -> None:
    decoded = decode_dashboard_payload(_binary_packet())

    assert decoded["packet_id"] == "sd_01_tk_0001_bearing_01_pkt_001"
    assert decoded["device_id"] == "machine_01"
    assert decoded["data"]["vibration"]["sample_count"] == 3
    assert "values" not in decoded["data"]["vibration"]
    assert decoded["data"]["bearing_module_temperature_c"] == 25.0


def test_decoded_binary_packet_counts_as_one_unique_dashboard_packet() -> None:
    dashboard = DashboardSession()
    event = {
        "type": "input-packet",
        "topic": "edge/edge_01/input",
        "payload": decode_dashboard_payload(_binary_packet()),
        "ts": 1.0,
    }

    assert dashboard.record(event) is None
    assert dashboard.stats["packet_receipts"] == 1
    assert dashboard.stats["packets"] == 1


def test_legacy_json_payload_remains_supported() -> None:
    payload = {"packet_id": "legacy_packet", "status": "normal"}

    assert decode_dashboard_payload(json.dumps(payload).encode("utf-8")) == payload


def test_truncated_binary_header_is_rejected() -> None:
    with pytest.raises(ValueError, match="header"):
        decode_dashboard_payload(b"IMC1\x10\x00\x00\x00{}")
