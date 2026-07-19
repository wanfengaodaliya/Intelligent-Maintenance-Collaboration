from __future__ import annotations

import os
import json
import unittest
from unittest.mock import Mock, patch

import requests

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


def vllm_settings(api_key: str = "") -> CloudSettings:
    return CloudSettings(
        backend="vllm",
        vllm_url="http://model.test/v1/chat/completions",
        vllm_model_name="qwen-cloud",
        vllm_api_key=api_key,
        vllm_timeout_seconds=9.5,
    )


def vllm_response(content: str) -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    return response


class VllmBackendTests(unittest.TestCase):
    def test_valid_model_json_becomes_cloud_result(self) -> None:
        model_content = json.dumps(
            {
                "label": "abnormal",
                "confidence": 0.94,
                "risk_level": "high",
                "action": "send_alert",
                "description": "云端确认异常",
            },
            ensure_ascii=False,
        )

        with patch(
            "cloud_service.vllm_backend.requests.post",
            return_value=vllm_response(model_content),
        ) as post:
            result = infer_cloud(
                make_valid_cloud_request(),
                settings=vllm_settings("test-key"),
            )

        self.assertEqual(result["packet_id"], "batch_000001")
        self.assertEqual(result["device_id"], "K001")
        self.assertEqual(result["cloud_node_id"], "cloud_1")
        self.assertEqual(result["model_name"], "qwen-cloud")
        self.assertEqual(result["label"], "abnormal")
        self.assertEqual(result["confidence"], 0.94)
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(result["decision"]["action"], "send_alert")
        self.assertEqual(result["decision"]["description"], "云端确认异常")
        self.assertGreaterEqual(result["cloud_latency_ms"], 0)

        _, call_kwargs = post.call_args
        self.assertEqual(call_kwargs["timeout"], 9.5)
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call_kwargs["json"]["model"], "qwen-cloud")
        self.assertEqual(call_kwargs["json"]["temperature"], 0.1)
        self.assertEqual(call_kwargs["json"]["max_tokens"], 512)
        self.assertEqual(call_kwargs["json"]["messages"][0]["role"], "system")

    def test_empty_api_key_omits_authorization_header(self) -> None:
        content = json.dumps(
            {
                "label": "normal",
                "confidence": 0.91,
                "risk_level": "low",
                "action": "record_only",
                "description": "状态正常",
            },
            ensure_ascii=False,
        )
        with patch(
            "cloud_service.vllm_backend.requests.post",
            return_value=vllm_response(content),
        ) as post:
            infer_cloud(make_valid_cloud_request("normal"), settings=vllm_settings())

        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_timeout_is_cloud_unavailable(self) -> None:
        with patch(
            "cloud_service.vllm_backend.requests.post",
            side_effect=requests.Timeout("slow"),
        ):
            with self.assertRaises(CloudServiceError) as captured:
                infer_cloud(make_valid_cloud_request(), settings=vllm_settings())

        self.assertEqual(captured.exception.code, "CLOUD_UNAVAILABLE")
        self.assertEqual(captured.exception.status_code, 503)

    def test_connection_error_is_cloud_unavailable(self) -> None:
        with patch(
            "cloud_service.vllm_backend.requests.post",
            side_effect=requests.ConnectionError("offline"),
        ):
            with self.assertRaises(CloudServiceError) as captured:
                infer_cloud(make_valid_cloud_request(), settings=vllm_settings())

        self.assertEqual(captured.exception.code, "CLOUD_UNAVAILABLE")
        self.assertEqual(captured.exception.status_code, 503)

    def test_http_error_is_cloud_unavailable(self) -> None:
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("500")
        with patch(
            "cloud_service.vllm_backend.requests.post",
            return_value=response,
        ):
            with self.assertRaises(CloudServiceError) as captured:
                infer_cloud(make_valid_cloud_request(), settings=vllm_settings())

        self.assertEqual(captured.exception.code, "CLOUD_UNAVAILABLE")
        self.assertEqual(captured.exception.status_code, 503)

    def test_markdown_fenced_json_is_rejected(self) -> None:
        with patch(
            "cloud_service.vllm_backend.requests.post",
            return_value=vllm_response('```json\n{"label":"normal"}\n```'),
        ):
            with self.assertRaises(CloudServiceError) as captured:
                infer_cloud(make_valid_cloud_request(), settings=vllm_settings())

        self.assertEqual(captured.exception.code, "MODEL_INFER_FAILED")
        self.assertEqual(captured.exception.status_code, 502)

    def test_empty_model_response_is_rejected(self) -> None:
        with patch(
            "cloud_service.vllm_backend.requests.post",
            return_value=vllm_response(""),
        ):
            with self.assertRaises(CloudServiceError) as captured:
                infer_cloud(make_valid_cloud_request(), settings=vllm_settings())

        self.assertEqual(captured.exception.code, "MODEL_INFER_FAILED")
        self.assertEqual(captured.exception.status_code, 502)

    def test_invalid_model_fields_are_rejected(self) -> None:
        invalid_cases = [
            {
                "label": "warning",
                "confidence": 0.9,
                "risk_level": "high",
                "action": "send_alert",
                "description": "bad label",
            },
            {
                "label": "abnormal",
                "confidence": 1.2,
                "risk_level": "high",
                "action": "send_alert",
                "description": "bad confidence",
            },
            {
                "label": "abnormal",
                "confidence": 0.9,
                "risk_level": "severe",
                "action": "send_alert",
                "description": "bad risk",
            },
            {
                "label": "abnormal",
                "confidence": 0.9,
                "risk_level": "high",
                "action": "restart_everything",
                "description": "bad action",
            },
        ]

        for invalid in invalid_cases:
            with self.subTest(invalid=invalid):
                with patch(
                    "cloud_service.vllm_backend.requests.post",
                    return_value=vllm_response(json.dumps(invalid)),
                ):
                    with self.assertRaises(CloudServiceError) as captured:
                        infer_cloud(
                            make_valid_cloud_request(),
                            settings=vllm_settings(),
                        )
                self.assertEqual(captured.exception.code, "MODEL_INFER_FAILED")
                self.assertEqual(captured.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
