"""Runtime settings for the cloud inference backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _artifact_path(env_name: str, relative_path: str) -> Path:
    configured = os.getenv(env_name)
    if configured:
        return Path(configured).expanduser()
    return PROJECT_ROOT / relative_path


@dataclass(frozen=True)
class CloudSettings:
    backend: str
    vllm_url: str
    vllm_model_name: str
    vllm_api_key: str
    vllm_timeout_seconds: float
    database_path: Path = PROJECT_ROOT / "data/cloud_review.db"
    legacy_bearing_window_review_enabled: bool = False
    legacy_context_enhanced_pipeline_enabled: bool = False
    moment_checkpoint_path: Path = PROJECT_ROOT / (
        "model_assets/moment/releases/moment-scl05-final/best_model.pt"
    )
    moment_condition_norm_path: Path = PROJECT_ROOT / (
        "model_assets/moment/releases/moment-scl05-final/condition_norm.json"
    )
    moment_pretrained_path: Path = PROJECT_ROOT / (
        "model_assets/moment/pretrained/MOMENT-1-small"
    )
    moment_deployment_dir: Path = PROJECT_ROOT / (
        "model_assets/moment/releases/moment-scl05-final"
    )
    moment_device: str = "auto"
    global_analysis_poll_seconds: float = 60.0


def load_cloud_settings() -> CloudSettings:
    """Load cloud backend settings from environment variables."""

    backend = os.getenv("CLOUD_BACKEND", "moment_light_adapt").strip().lower()
    if backend != "moment_light_adapt":
        raise ValueError(
            f"unsupported cloud backend: {backend}; only moment_light_adapt is supported"
        )
    return CloudSettings(
        backend=backend,
        vllm_url=os.getenv(
            "VLLM_URL",
            "http://127.0.0.1:6006/v1/chat/completions",
        ).strip(),
        vllm_model_name=os.getenv("VLLM_MODEL_NAME", "qwen-cloud").strip(),
        vllm_api_key=os.getenv("VLLM_API_KEY", "").strip(),
        vllm_timeout_seconds=float(os.getenv("VLLM_TIMEOUT_SECONDS", "120")),
        database_path=_artifact_path("CLOUD_REVIEW_DB_PATH", "data/cloud_review.db"),
        legacy_bearing_window_review_enabled=(
            os.getenv("CLOUD_LEGACY_BEARING_WINDOW_REVIEW_ENABLED", "false")
            .strip()
            .lower()
            == "true"
        ),
        legacy_context_enhanced_pipeline_enabled=(
            os.getenv("CLOUD_LEGACY_CONTEXT_ENHANCED_PIPELINE_ENABLED", "false")
            .strip()
            .lower()
            == "true"
        ),
        moment_checkpoint_path=_artifact_path(
            "CLOUD_MOMENT_CHECKPOINT_PATH",
            "model_assets/moment/releases/moment-scl05-final/best_model.pt",
        ),
        moment_condition_norm_path=_artifact_path(
            "CLOUD_MOMENT_CONDITION_NORM_PATH",
            "model_assets/moment/releases/moment-scl05-final/condition_norm.json",
        ),
        moment_pretrained_path=_artifact_path(
            "CLOUD_MOMENT_PRETRAINED_PATH",
            "model_assets/moment/pretrained/MOMENT-1-small",
        ),
        moment_deployment_dir=_artifact_path(
            "CLOUD_MOMENT_DEPLOYMENT_DIR",
            "model_assets/moment/releases/moment-scl05-final",
        ),
        moment_device=os.getenv("CLOUD_MOMENT_DEVICE", "auto").strip().lower(),
        global_analysis_poll_seconds=float(
            os.getenv("GLOBAL_ANALYSIS_POLL_SECONDS", "60")
        ),
    )
