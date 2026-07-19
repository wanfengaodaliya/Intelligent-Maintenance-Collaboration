from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from common.config import service_url


class ServiceUrlTests(unittest.TestCase):
    def test_cloud_service_url_environment_override(self) -> None:
        with patch.dict(
            os.environ,
            {"CLOUD_SERVICE_URL": "https://cloud.example.test/"},
            clear=True,
        ):
            result = service_url("cloud")

        self.assertEqual(result, "https://cloud.example.test")

    def test_non_cloud_service_ignores_cloud_override(self) -> None:
        with patch.dict(
            os.environ,
            {"CLOUD_SERVICE_URL": "https://cloud.example.test"},
            clear=True,
        ):
            result = service_url("edge")

        self.assertEqual(result, "http://127.0.0.1:8001")


if __name__ == "__main__":
    unittest.main()
