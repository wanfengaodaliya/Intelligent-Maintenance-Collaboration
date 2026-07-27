"""Runtime settings for the cloud inference backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CloudSettings:
    backend: str
    vllm_url: str
    vllm_model_name: str
    vllm_api_key: str
    vllm_timeout_seconds: float
    database_path: Path = Path("data/cloud_review.db")


def load_cloud_settings() -> CloudSettings:
    """Load cloud backend settings from environment variables."""

    return CloudSettings(
        backend=os.getenv("CLOUD_BACKEND", "mock").strip().lower(),
        vllm_url=os.getenv(
            "VLLM_URL",
            "http://127.0.0.1:6006/v1/chat/completions",
        ).strip(),
        vllm_model_name=os.getenv("VLLM_MODEL_NAME", "qwen-cloud").strip(),
        vllm_api_key=os.getenv("VLLM_API_KEY", "").strip(),
        vllm_timeout_seconds=float(os.getenv("VLLM_TIMEOUT_SECONDS", "120")),
        database_path=Path(os.getenv("CLOUD_REVIEW_DB_PATH", "data/cloud_review.db")),
    )
