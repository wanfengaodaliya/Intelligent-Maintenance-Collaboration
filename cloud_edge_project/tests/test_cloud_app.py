from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import requests
from fastapi.responses import JSONResponse

from cloud_service import app as cloud_app
from cloud_service.errors import CloudServiceError
from tests.helpers import make_valid_cloud_request


def response_body(response: JSONResponse) -> dict:
    return json.loads(response.body.decode("utf-8"))


class CloudAppTests(unittest.TestCase):
    def test_mock_health_reports_selected_backend(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = cloud_app.health()

        self.assertEqual(result["service"], "cloud_service")
        self.assertEqual(result["node_id"], "cloud_1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_backend"], "mock")

    def test_vllm_health_reports_unavailable(self) -> None:
        with (
            patch.dict(os.environ, {"CLOUD_BACKEND": "vllm"}, clear=True),
            patch(
                "cloud_service.app.requests.get",
                side_effect=requests.ConnectionError("offline"),
            ),
        ):
            result = cloud_app.health()

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(response_body(result)["status"], "unavailable")
        self.assertEqual(response_body(result)["model_backend"], "vllm")

    def test_cloud_service_error_preserves_status_and_packet_id(self) -> None:
        request = make_valid_cloud_request()
        error = CloudServiceError(
            "CLOUD_UNAVAILABLE",
            "vLLM service is unavailable",
            503,
        )
        with patch("cloud_service.app.infer_cloud", side_effect=error):
            result = cloud_app.cloud_infer(request)

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 503)
        body = response_body(result)
        self.assertFalse(body["success"])
        self.assertEqual(body["packet_id"], "batch_000001")
        self.assertEqual(body["error_code"], "CLOUD_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
