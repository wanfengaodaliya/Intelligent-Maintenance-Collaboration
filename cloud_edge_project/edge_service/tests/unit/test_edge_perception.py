# -*- coding: utf-8 -*-
"""降采样与单包轴承感知单元测试（也可用 unittest 直接运行）。"""
from __future__ import annotations

import copy
import math
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np


_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from edge_perception import (  # noqa: E402
    ConstantDetectionConfig,
    EdgePerception,
    PerceptionConfig,
    PerceptionInvocationContext,
    file_sha256,
)
from model_input_contract import validate_model_input  # noqa: E402


_ASSET = _SRC / "edge_perception" / "assets" / "fir_64k_to_16k_369.txt"


def _config(**overrides) -> PerceptionConfig:
    values = {
        "profile": "development_test",
        "fir_coefficients_path": _ASSET,
        "fir_sha256": file_sha256(_ASSET),
        "fir_asset_source": "development_test",
        "fir_asset_version": "dev-v1",
        "running_speed_threshold_rpm": 100.0,
        "running_speed_threshold_source": "development_test",
        "running_speed_threshold_version": "dev-v1",
        "constant_detection": {
            "vibration": ConstantDetectionConfig(True, 1e-9, "development_test", "dev-v1"),
            "phase_current_1_A": ConstantDetectionConfig(True, 1e-9, "development_test", "dev-v1"),
            "phase_current_2_A": ConstantDetectionConfig(True, 1e-9, "development_test", "dev-v1"),
        },
        "feature_zero_rms_threshold": 1e-10,
        "feature_zero_power_threshold": 1e-20,
        "current_relationship_zero_rms_threshold": 1e-10,
        "numerical_threshold_source": "development_test",
        "numerical_threshold_version": "dev-v1",
        "feature_extractor_version": "perception-dev-v1",
        "runtime_dependencies": {"numpy": np.__version__},
        "absolute_tolerance": 1e-12,
        "relative_tolerance": 1e-9,
    }
    values.update(overrides)
    return PerceptionConfig(**values)


def _channel(values, sample_rate, unit=None):
    result = {
        "sample_rate_hz": sample_rate,
        "sample_count": len(values),
        "values": [float(value) for value in values],
    }
    if unit is not None:
        result["unit"] = unit
    return result


def _raw_packet(*, sender="sender-1", bearing="bearing-1", speed=1500.0):
    high_t = np.arange(3200, dtype=np.float64) / 64000.0
    low_t = np.arange(200, dtype=np.float64) / 4000.0
    vibration = 3.0 + 2.0 * np.sin(2.0 * np.pi * 1000.0 * high_t)
    current_1 = 10.0 + 2.0 * math.sqrt(2.0) * np.sin(2.0 * np.pi * 200.0 * high_t)
    current_2 = -4.0 + math.sqrt(2.0) * np.sin(2.0 * np.pi * 400.0 * high_t)
    return {
        "device_id": "device-1",
        "bearing_id": bearing,
        "sender_id": sender,
        "task_id": f"task-{sender}",
        "packet_id": f"packet-{sender}",
        "sequence_number": 1,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000,
        "data": {
            "vibration": _channel(vibration, 64000, "mm/s"),
            "phase_current_1_A": _channel(current_1, 64000, "A"),
            "phase_current_2_A": _channel(current_2, 64000, "A"),
            "shaft_speed_rpm": _channel(np.full(200, speed), 4000),
            "load_torque_nm": _channel(10.0 + low_t, 4000),
            "bearing_radial_load_n": _channel(np.linspace(100.0, 200.0, 200), 4000),
            "bearing_module_temperature_c": 46.3,
        },
    }


class EdgePerceptionTests(unittest.TestCase):
    def setUp(self):
        self.received_ns = 1_800_000_000_000_000_000
        self.generated_ns = self.received_ns + 25_000_000
        self.context = PerceptionInvocationContext("edge-1", self.received_ns)
        self.logs = []
        self.processor = EdgePerception(
            _config(), clock_ns=lambda: self.generated_ns, on_error=self.logs.append
        )

    def _run(self, raw=None):
        downsampled = self.processor.downsample(raw or _raw_packet(), self.context)
        self.assertTrue(downsampled.status.success)
        perception = self.processor.perceive(downsampled.payload, self.context)
        return downsampled, perception

    def test_downsampling_and_features_match_known_signals(self):
        downsampled, perception = self._run()
        self.assertTrue(perception.status.success)
        result = perception.payload
        validate_model_input(result)
        vibration = result["features"]["vibration"]
        current_1 = result["features"]["phase_current_1"]
        current_2 = result["features"]["phase_current_2"]

        self.assertEqual(downsampled.payload["data"]["vibration"]["values"].shape, (800,))
        self.assertFalse(downsampled.payload["data"]["vibration"]["values"].flags.writeable)
        self.assertAlmostEqual(vibration["rms"], math.sqrt(2.0), places=4)
        self.assertAlmostEqual(vibration["absolute_peak"], 2.0, delta=0.02)
        self.assertAlmostEqual(vibration["kurtosis"], 1.5, places=3)
        self.assertEqual(vibration["dominant_frequency_hz"], 1000.0)
        self.assertGreater(vibration["band_power_ratio_500_2000"], 0.999)
        self.assertAlmostEqual(current_1["rms_a"], 2.0, places=4)
        self.assertAlmostEqual(current_2["rms_a"], 1.0, places=4)
        self.assertAlmostEqual(
            result["features"]["current_relationship"]["current_imbalance_ratio"],
            2.0 / 3.0,
            places=4,
        )
        self.assertEqual(result["feature_generated_at_ns"], self.generated_ns)

    def test_identity_is_preserved_and_raw_packet_is_not_modified(self):
        raw = _raw_packet()
        before = copy.deepcopy(raw)
        downsampled, perception = self._run(raw)
        self.assertEqual(raw, before)
        for field in ("device_id", "bearing_id", "sender_id", "task_id", "packet_id"):
            self.assertEqual(downsampled.payload[field], raw[field])
            self.assertEqual(perception.payload[field], raw[field])

    def test_centered_fir_preserves_fixed_decimation_phase(self):
        raw = _raw_packet()
        impulse = [0.0] * 3200
        impulse[400] = 1.0
        for channel in ("vibration", "phase_current_1_A", "phase_current_2_A"):
            raw["data"][channel]["values"] = impulse.copy()
        downsampled = self.processor.downsample(raw, self.context)
        self.assertTrue(downsampled.status.success)
        for channel in ("vibration", "phase_current_1_A", "phase_current_2_A"):
            values = downsampled.payload["data"][channel]["values"]
            self.assertEqual(int(np.argmax(np.abs(values))), 100)

    def test_operating_context_uses_population_statistics(self):
        raw = _raw_packet()
        raw["data"]["shaft_speed_rpm"]["values"] = [float(i) for i in range(200)]
        _, perception = self._run(raw)
        stats = perception.payload["features"]["operating_context"]["shaft_speed_rpm"]
        expected = np.arange(200, dtype=np.float64)
        self.assertEqual(stats["mean"], float(np.mean(expected)))
        self.assertEqual(stats["last"], 199.0)
        self.assertEqual(stats["minimum"], 0.0)
        self.assertEqual(stats["maximum"], 199.0)
        self.assertEqual(stats["standard_deviation"], float(np.std(expected, ddof=0)))

    def test_not_running_is_warning_but_still_produces_features(self):
        _, perception = self._run(_raw_packet(speed=50.0))
        self.assertTrue(perception.status.success)
        self.assertEqual(perception.payload["perception_quality"], {
            "status": "warning",
            "flags": ["DEVICE_NOT_RUNNING"],
        })

    def test_constant_current_is_flagged_while_vibration_features_succeed(self):
        raw = _raw_packet()
        raw["data"]["phase_current_1_A"]["values"] = [5.0] * 3200
        _, perception = self._run(raw)
        self.assertTrue(perception.status.success)
        self.assertIn(
            "PHASE_CURRENT_1_CONSTANT_SIGNAL",
            perception.payload["perception_quality"]["flags"],
        )

    def test_zero_vibration_fails_without_partial_payload_and_logs_scope(self):
        raw = _raw_packet()
        raw["data"]["vibration"]["values"] = [0.0] * 3200
        downsampled = self.processor.downsample(raw, self.context)
        perception = self.processor.perceive(downsampled.payload, self.context)
        self.assertFalse(perception.status.success)
        self.assertEqual(perception.status.error_code, "PERCEPTION_FAILED")
        self.assertIsNone(perception.payload)
        self.assertEqual(self.logs[-1]["scope"], "INSUFFICIENT_SIGNAL_POWER")
        self.assertNotIn("values", self.logs[-1])

    def test_near_zero_currents_use_defined_ratio_and_increment_metric(self):
        raw = _raw_packet()
        raw["data"]["phase_current_1_A"]["values"] = [0.0] * 3200
        raw["data"]["phase_current_2_A"]["values"] = [0.0] * 3200
        _, perception = self._run(raw)
        self.assertTrue(perception.status.success)
        self.assertEqual(
            perception.payload["features"]["current_relationship"]["current_imbalance_ratio"],
            0.0,
        )
        self.assertEqual(self.processor.near_zero_current_count, 1)

    def test_missing_bearing_id_fails_downsampling(self):
        raw = _raw_packet()
        raw.pop("bearing_id")
        result = self.processor.downsample(raw, self.context)
        self.assertFalse(result.status.success)
        self.assertEqual(result.status.error_code, "DOWNSAMPLING_FAILED")
        self.assertIsNone(result.payload)
        self.assertEqual(self.logs[-1]["scope"], "bearing_id")

    def test_two_senders_with_same_sequence_are_isolated(self):
        first_down, first = self._run(_raw_packet(sender="sender-a", bearing="bearing-a"))
        second_down, second = self._run(_raw_packet(sender="sender-b", bearing="bearing-b"))
        self.assertEqual(first_down.payload["sequence_number"], second_down.payload["sequence_number"])
        self.assertEqual(first.payload["bearing_id"], "bearing-a")
        self.assertEqual(first.payload["sender_id"], "sender-a")
        self.assertEqual(second.payload["bearing_id"], "bearing-b")
        self.assertEqual(second.payload["sender_id"], "sender-b")

    def test_parallel_senders_do_not_cross_identity(self):
        def process(index):
            raw = _raw_packet(sender=f"sender-{index}", bearing=f"bearing-{index}")
            downsampled = self.processor.downsample(raw, self.context)
            perception = self.processor.perceive(downsampled.payload, self.context)
            return perception.payload["bearing_id"], perception.payload["sender_id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(process, range(12)))
        self.assertEqual(
            results,
            [(f"bearing-{index}", f"sender-{index}") for index in range(12)],
        )

    def test_invalid_development_source_is_rejected(self):
        config = _config(running_speed_threshold_source="healthy_data")
        with self.assertRaisesRegex(ValueError, "development_test"):
            EdgePerception(config)

    def test_fir_hash_mismatch_is_rejected(self):
        config = _config(fir_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            EdgePerception(config)

    def test_generated_time_before_received_time_fails(self):
        processor = EdgePerception(
            _config(), clock_ns=lambda: self.received_ns - 1, on_error=self.logs.append
        )
        downsampled = processor.downsample(_raw_packet(), self.context)
        perception = processor.perceive(downsampled.payload, self.context)
        self.assertFalse(perception.status.success)
        self.assertIsNone(perception.payload)
        self.assertEqual(self.logs[-1]["scope"], "feature_generated_at_ns")


if __name__ == "__main__":
    unittest.main()
