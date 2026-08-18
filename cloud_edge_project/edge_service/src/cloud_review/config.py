"""Configuration for edge-owned cloud-review persistence and HTTP clients."""
# 该模块加载边缘侧云端复核存储和 HTTP 客户端配置。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from common.config import load_config


@dataclass(frozen=True)
class CloudReviewConfig:
    cache_directory: Path
    cloud_base_url: str
    scheduler_base_url: str
    timeout_seconds: float = 3.0
    connect_timeout_seconds: float = 0.5
    read_timeout_seconds: float = 2.5
    retention_ns: int = 86_400_000_000_000
    cleanup_interval_seconds: float = 60.0

    def request_timeout(self) -> tuple[float, float]:
        """HTTP 连接与读取超时分开的 (connect, read) 预算。"""
        return (self.connect_timeout_seconds, self.read_timeout_seconds)


def load_cloud_review_config() -> CloudReviewConfig:
    project_root = Path(__file__).resolve().parents[3]
    deferred = load_config().get("deferred_cloud_review", {})
    return CloudReviewConfig(
        cache_directory=Path(
            os.getenv(
                "EDGE_CLOUD_REVIEW_CACHE_DIR",
                str(project_root / "data" / "edge_cloud_review"),
            )
        ),
        cloud_base_url=os.getenv("CLOUD_SERVICE_BASE_URL", "http://127.0.0.1:18021").rstrip("/"),
        scheduler_base_url=os.getenv("SCHEDULER_SERVICE_BASE_URL", "http://127.0.0.1:18011").rstrip("/"),
        timeout_seconds=float(os.getenv("EDGE_CLOUD_REVIEW_TIMEOUT_SECONDS", "3")),
        connect_timeout_seconds=float(
            os.getenv(
                "EDGE_CLOUD_REVIEW_CONNECT_TIMEOUT_SECONDS",
                str(int(os.getenv("EDGE_HTTP_CONNECT_TIMEOUT_MS", "500")) / 1000),
            )
        ),
        read_timeout_seconds=float(
            os.getenv(
                "EDGE_CLOUD_REVIEW_READ_TIMEOUT_SECONDS",
                str(int(os.getenv("EDGE_HTTP_READ_TIMEOUT_MS", "2000")) / 1000),
            )
        ),
        retention_ns=int(
            os.getenv(
                "EDGE_CLOUD_REVIEW_RETENTION_NS",
                str(int(float(deferred.get("retention_hours", 24)) * 3_600_000_000_000)),
            )
        ),
        cleanup_interval_seconds=float(
            os.getenv(
                "EDGE_CLOUD_REVIEW_CLEANUP_SECONDS",
                str(deferred.get("cleanup_interval_seconds", 60.0)),
            )
        ),
    )
