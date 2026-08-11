"""Periodic cloud-node status reporting to the scheduler control plane."""
# 该模块定期向调度器上报云节点状态和边云链路快照。

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Callable

import requests

from .config import CloudSettings


def build_status_payload(
    settings: CloudSettings,
    *,
    cloud_node_id: str,
    status_message_id: str,
    reported_at_ns: int,
    health_status: str,
    queue_length: int,
    model_load_status: str,
    last_task_activity_ns: int,
) -> dict[str, Any]:
    return {
        "status_message_id": status_message_id,
        "cloud_node_id": cloud_node_id,
        "reported_at_ns": reported_at_ns,
        "health_status": health_status,
        "resources": {
            "logical_cpu_count": max(int(os.cpu_count() or 1), 1),
            "cpu_utilization_percent": 0.0,
            "memory_available_mb": 0.0,
            "gpu_available": settings.backend == "vllm",
            "npu_available": False,
            "queue_length": queue_length,
        },
        "models": [
            {
                "model_version": settings.vllm_model_name or "cloud-model",
                "model_load_status": model_load_status,
            }
        ],
        "network_to_scheduler": {
            "measured_at_ns": reported_at_ns,
            "available_uplink_mbps_estimate": 0.0,
            "rtt_ms_avg": 0.0,
            "rtt_ms_p95": 0.0,
            "loss_rate": 0.0,
        },
        "last_task_activity_ns": last_task_activity_ns,
    }


class CloudNodeStatusReporter:
    def __init__(
        self,
        *,
        scheduler_base_url: str,
        cloud_node_id: str,
        settings_provider: Callable[[], CloudSettings],
        queue_length_provider: Callable[[], int],
        health_provider: Callable[[], tuple[str, str]],
        last_activity_provider: Callable[[], int],
        timeout_seconds: float = 1.0,
        interval_seconds: float = 1.0,
        clock_ns: Callable[[], int] = time.time_ns,
        message_id_factory: Callable[[], str] = lambda: f"cloud-status-{uuid.uuid4().hex}",
        http_post: Callable[..., Any] = requests.post,
    ) -> None:
        self.scheduler_base_url = scheduler_base_url.rstrip("/")
        self.cloud_node_id = cloud_node_id
        self.settings_provider = settings_provider
        self.queue_length_provider = queue_length_provider
        self.health_provider = health_provider
        self.last_activity_provider = last_activity_provider
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.clock_ns = clock_ns
        self.message_id_factory = message_id_factory
        self.http_post = http_post

    def report_once(self) -> bool:
        try:
            health_status, model_load_status = self.health_provider()
            payload = build_status_payload(
                self.settings_provider(),
                cloud_node_id=self.cloud_node_id,
                status_message_id=self.message_id_factory(),
                reported_at_ns=self.clock_ns(),
                health_status=health_status,
                queue_length=self.queue_length_provider(),
                model_load_status=model_load_status,
                last_task_activity_ns=self.last_activity_provider(),
            )
            response = self.http_post(
                self.scheduler_base_url + "/scheduler/cloud-nodes/status",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return True
        except (requests.RequestException, OSError, ValueError, TypeError):
            return False

    async def run_forever(self) -> None:
        while True:
            await asyncio.to_thread(self.report_once)
            await asyncio.sleep(self.interval_seconds)
