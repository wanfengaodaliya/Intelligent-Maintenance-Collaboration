from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


class DeploymentScriptTests(unittest.TestCase):
    def test_start_vllm_uses_autodl_model_and_internal_port(self) -> None:
        script = (SCRIPTS_DIR / "start_vllm.sh").read_text(encoding="utf-8")

        self.assertIn("activate cloud_llm", script)
        self.assertIn("/root/autodl-tmp/models/Qwen3-14B-AWQ", script)
        self.assertIn("--host 127.0.0.1", script)
        self.assertIn("--port 6006", script)
        self.assertIn("--served-model-name qwen-cloud", script)
        self.assertNotIn("cloud-edge-key", script)

    def test_start_cloud_service_selects_vllm_and_repo_directory(self) -> None:
        script = (SCRIPTS_DIR / "start_cloud_service.sh").read_text(
            encoding="utf-8",
        )

        self.assertIn("activate cloud_llm", script)
        self.assertIn('export CLOUD_BACKEND="vllm"', script)
        self.assertIn('export CLOUD_SERVICE_PORT="6008"', script)
        self.assertIn("BASH_SOURCE[0]", script)
        self.assertIn("cloud_service.app:app", script)
        self.assertIn("--host 0.0.0.0", script)
        self.assertIn("--port 6008", script)
        self.assertNotIn("VLLM_API_KEY=", script)


if __name__ == "__main__":
    unittest.main()
