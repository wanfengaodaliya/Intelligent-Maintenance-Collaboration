from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from cloud_service.config import CloudSettings
from cloud_service.errors import CloudServiceError
from cloud_service.model import infer_cloud
from tests.helpers import make_valid_cloud_request


class CloudServiceSelectionTests(unittest.TestCase):
    def test_default_backend_uses_existing_mock_contract(self) -> None:
        request = make_valid_cloud_request()

        with patch.dict(os.environ, {}, clear=True):
            result = infer_cloud(request)

        self.assertEqual(result["packet_id"], request["packet"]["packet_id"])
        self.assertEqual(result["device_id"], request["packet"]["device_id"])
        self.assertEqual(result["model_name"], "cloud_bearing_mock")
        self.assertEqual(result["label"], "abnormal")
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(result["decision"]["action"], "send_alert")

    def test_unknown_backend_does_not_fall_back_to_mock(self) -> None:
        settings = CloudSettings(
            backend="unknown",
            vllm_url="http://unused",
            vllm_model_name="unused",
            vllm_api_key="",
            vllm_timeout_seconds=120,
        )

        with self.assertRaisesRegex(
            CloudServiceError,
            "unsupported cloud backend: unknown",
        ) as captured:
            infer_cloud(make_valid_cloud_request(), settings=settings)

        self.assertEqual(captured.exception.code, "MODEL_INFER_FAILED")
        self.assertEqual(captured.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
