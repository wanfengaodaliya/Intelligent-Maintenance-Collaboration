from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from cloud_service.config import load_cloud_settings
from cloud_service.prompt import build_cloud_messages


def make_cloud_request(vibration: list[float]) -> dict:
    return {
        "packet": {
            "packet_id": "batch_000001",
            "device_id": "K001",
            "sensor_id": "sensor_K001",
            "data": {
                "vibration": vibration,
                "current": 1.34,
                "temperature": 45.8,
                "speed": 899.7,
                "load": 0.7,
            },
        },
        "edge_result": {
            "label": "abnormal",
            "confidence": 0.72,
            "risk_level": "medium",
        },
    }


class CloudSettingsTests(unittest.TestCase):
    def test_default_settings_use_mock(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_cloud_settings()

        self.assertEqual(settings.backend, "mock")
        self.assertEqual(
            settings.vllm_url,
            "http://127.0.0.1:6006/v1/chat/completions",
        )
        self.assertEqual(settings.vllm_model_name, "qwen-cloud")
        self.assertEqual(settings.vllm_api_key, "")
        self.assertEqual(settings.vllm_timeout_seconds, 120.0)

    def test_environment_overrides_all_settings(self) -> None:
        environment = {
            "CLOUD_BACKEND": "VLLM",
            "VLLM_URL": "http://model.test/v1/chat/completions",
            "VLLM_MODEL_NAME": "test-model",
            "VLLM_API_KEY": "secret",
            "VLLM_TIMEOUT_SECONDS": "9.5",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_cloud_settings()

        self.assertEqual(settings.backend, "vllm")
        self.assertEqual(settings.vllm_url, environment["VLLM_URL"])
        self.assertEqual(settings.vllm_model_name, "test-model")
        self.assertEqual(settings.vllm_api_key, "secret")
        self.assertEqual(settings.vllm_timeout_seconds, 9.5)


class CloudPromptTests(unittest.TestCase):
    def test_build_messages_compacts_vibration_as_json(self) -> None:
        messages = build_cloud_messages(make_cloud_request([1.0, -1.0] * 400))

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("JSON", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["packet"]["packet_id"], "batch_000001")
        self.assertEqual(payload["vibration_summary"]["count"], 800)
        self.assertEqual(payload["vibration_summary"]["min"], -1.0)
        self.assertEqual(payload["vibration_summary"]["max"], 1.0)
        self.assertEqual(payload["vibration_summary"]["mean"], 0.0)
        self.assertEqual(payload["vibration_summary"]["rms"], 1.0)
        self.assertEqual(payload["vibration_summary"]["peak_abs"], 1.0)
        self.assertNotIn("vibration", payload["sensor_data"])
        self.assertEqual(payload["sensor_data"]["temperature"], 45.8)
        self.assertEqual(payload["edge_result"]["label"], "abnormal")


if __name__ == "__main__":
    unittest.main()
