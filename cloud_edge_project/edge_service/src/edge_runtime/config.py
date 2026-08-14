# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


_EDGE_NODE_ID = re.compile(r"^edge_\d{2,}$")


@dataclass(frozen=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    keepalive_seconds: int = 30
    qos: int = 1
    input_topic: str = "edge/edge_01/input"
    device_result_topic: str = "summary/device-results"
    client_id: str = "edge_01-runtime"
    ingress_queue_capacity: int = 160


@dataclass(frozen=True)
class SchedulerConfig:
    base_url: str = "http://127.0.0.1:8003"
    status_path: str = "/scheduler/edge-nodes/status"
    request_timeout_seconds: float = 2.0
    heartbeat_interval_seconds: float = 1.0


@dataclass(frozen=True)
class ControlServerConfig:
    host: str = "0.0.0.0"
    port: int = 8011


@dataclass(frozen=True)
class WindowTransferConfig:
    cache_directory: Path = Path("data/edge_bearing_windows")
    cloud_base_url: str = "http://127.0.0.1:8004"
    hard_limit_bytes: int = 20 * 1024**3
    warning_bytes: int = 16 * 1024**3
    reserved_free_bytes: int = 10 * 1024**3
    dispatch_interval_seconds: float = 1.0
    packet_cloud_confidence_threshold: float = 0.0


@dataclass(frozen=True)
class V12RuntimeConfig:
    enabled: bool = True
    database_path: Path = Path("data/edge_v12.db")
    legacy_realtime_aggregation: bool = False
    cloud_now_timeout_ms: int = 3_000
    round_finalize_grace_ms: int = 500
    round_timeout_ms: int = 3_500
    diagnosis_window_ms: int = 50
    diagnosis_step_ms: int = 50
    diagnosis_overlap_enabled: bool = False


@dataclass(frozen=True)
class RawSampleCaptureConfig:
    enabled: bool = True
    directory: Path = Path("data/raw_analysis_samples")
    history_window_ms: int = 1_000
    normal_sample_interval_seconds: int = 60
    local_retention_hours: int = 24
    max_local_storage_mb: int = 2_048
    upload_batch_size: int = 1


@dataclass(frozen=True)
class EdgeRuntimeConfig:
    edge_node_id: str = "edge_01"
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    control: ControlServerConfig = field(default_factory=ControlServerConfig)
    window_transfer: WindowTransferConfig = field(default_factory=WindowTransferConfig)
    v12: V12RuntimeConfig = field(default_factory=V12RuntimeConfig)
    raw_sample_capture: RawSampleCaptureConfig = field(default_factory=RawSampleCaptureConfig)
    cloud_node_urls: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EdgeRuntimeConfig":
        env = os.environ if environ is None else environ
        edge_node_id = env.get("EDGE_NODE_ID", "edge_01").strip()
        cloud_node_urls = json.loads(env.get("EDGE_CLOUD_NODE_URLS_JSON", "{}"))
        if not isinstance(cloud_node_urls, dict):
            raise ValueError("EDGE_CLOUD_NODE_URLS_JSON must be a JSON object")
        return cls(
            edge_node_id=edge_node_id,
            mqtt=MqttConfig(
                host=env.get("EDGE_MQTT_HOST", "127.0.0.1").strip(),
                port=int(env.get("EDGE_MQTT_PORT", "1883")),
                input_topic=env.get(
                    "EDGE_MQTT_INPUT_TOPIC",
                    f"edge/{edge_node_id}/input",
                ).strip(),
                device_result_topic=env.get(
                    "EDGE_MQTT_DEVICE_RESULT_TOPIC",
                    "summary/device-results",
                ).strip(),
                client_id=env.get(
                    "EDGE_MQTT_CLIENT_ID",
                    f"{edge_node_id}-runtime",
                ).strip(),
            ),
            scheduler=SchedulerConfig(
                base_url=env.get(
                    "SCHEDULER_SERVICE_BASE_URL",
                    "http://127.0.0.1:8003",
                ).rstrip("/"),
            ),
            control=ControlServerConfig(
                host=env.get("EDGE_CONTROL_HOST", "0.0.0.0").strip(),
                port=int(env.get("EDGE_CONTROL_PORT", "8011")),
            ),
            v12=V12RuntimeConfig(
                enabled=env.get("EDGE_V12_ENABLED", "true").strip().lower() == "true",
                database_path=Path(env.get("EDGE_V12_DATABASE_PATH", "data/edge_v12.db")),
                legacy_realtime_aggregation=(
                    env.get("EDGE_LEGACY_REALTIME_AGGREGATION", "false").strip().lower() == "true"
                ),
                cloud_now_timeout_ms=int(env.get("EDGE_CLOUD_NOW_TIMEOUT_MS", "3000")),
                round_finalize_grace_ms=int(env.get("EDGE_ROUND_FINALIZE_GRACE_MS", "500")),
                round_timeout_ms=int(env.get("EDGE_ROUND_TIMEOUT_MS", "3500")),
                diagnosis_window_ms=int(env.get("EDGE_DIAGNOSIS_WINDOW_MS", "50")),
                diagnosis_step_ms=int(env.get("EDGE_DIAGNOSIS_STEP_MS", "50")),
                diagnosis_overlap_enabled=env.get("EDGE_DIAGNOSIS_OVERLAP_ENABLED", "false").strip().lower() == "true",
            ),
            raw_sample_capture=RawSampleCaptureConfig(
                enabled=env.get("EDGE_RAW_SAMPLE_CAPTURE_ENABLED", "true").strip().lower() == "true",
                directory=Path(env.get("EDGE_RAW_SAMPLE_DIRECTORY", "data/raw_analysis_samples")),
                history_window_ms=int(env.get("EDGE_RAW_SAMPLE_HISTORY_WINDOW_MS", "1000")),
                normal_sample_interval_seconds=int(env.get("EDGE_RAW_SAMPLE_NORMAL_INTERVAL_SECONDS", "60")),
                local_retention_hours=int(env.get("EDGE_RAW_SAMPLE_RETENTION_HOURS", "24")),
                max_local_storage_mb=int(env.get("EDGE_RAW_SAMPLE_MAX_STORAGE_MB", "2048")),
                upload_batch_size=int(env.get("EDGE_RAW_SAMPLE_UPLOAD_BATCH_SIZE", "1")),
            ),
            cloud_node_urls=cloud_node_urls,
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not _EDGE_NODE_ID.fullmatch(self.edge_node_id):
            errors.append("edge_node_id must match edge_<at least two digits>")
        expected_topic = f"edge/{self.edge_node_id}/input"
        if self.mqtt.input_topic != expected_topic:
            errors.append(f"mqtt.input_topic must equal {expected_topic}")
        if not isinstance(self.mqtt.port, int) or isinstance(self.mqtt.port, bool) \
                or not 1 <= self.mqtt.port <= 65535:
            errors.append("mqtt.port must be a valid port")
        if self.mqtt.qos != 1:
            errors.append("edge MQTT contract requires qos=1")
        if self.mqtt.ingress_queue_capacity < 1:
            errors.append("mqtt.ingress_queue_capacity must be positive")
        if not self.mqtt.host.strip() or not self.mqtt.client_id.strip():
            errors.append("MQTT host and client_id must be non-empty")
        if not self.mqtt.device_result_topic.strip():
            errors.append("mqtt.device_result_topic must be non-empty")
        if not 0.0 <= self.window_transfer.packet_cloud_confidence_threshold <= 1.0:
            errors.append("packet_cloud_confidence_threshold must be between 0 and 1")
        if not self.scheduler.base_url.startswith(("http://", "https://")):
            errors.append("scheduler.base_url must use HTTP or HTTPS")
        if self.scheduler.heartbeat_interval_seconds != 1.0:
            errors.append("heartbeat_interval_seconds must be fixed at one second")
        if self.scheduler.request_timeout_seconds <= 0:
            errors.append("scheduler.request_timeout_seconds must be positive")
        if not isinstance(self.control.port, int) or isinstance(self.control.port, bool) \
                or not 1 <= self.control.port <= 65535:
            errors.append("control.port must be a valid port")
        if not self.control.host.strip():
            errors.append("control.host must be non-empty")
        if any(
            not isinstance(node_id, str) or not node_id.strip()
            or not isinstance(url, str) or not url.startswith(("http://", "https://"))
            for node_id, url in self.cloud_node_urls.items()
        ):
            errors.append("cloud_node_urls must map node IDs to HTTP(S) URLs")
        transfer = self.window_transfer
        if not transfer.cloud_base_url.startswith(("http://", "https://")):
            errors.append("window_transfer.cloud_base_url must use HTTP or HTTPS")
        if not 0 < transfer.warning_bytes < transfer.hard_limit_bytes:
            errors.append("window transfer warning limit must be below the hard limit")
        if transfer.reserved_free_bytes < 0 or transfer.dispatch_interval_seconds <= 0:
            errors.append("window transfer reserve and interval are invalid")
        if self.v12.enabled and not str(self.v12.database_path).strip():
            errors.append("v12.database_path must be non-empty")
        if self.v12.cloud_now_timeout_ms <= 0 or self.v12.round_finalize_grace_ms < 0:
            errors.append("v12 cloud-now timeout and finalize grace are invalid")
        if self.v12.round_timeout_ms < self.v12.cloud_now_timeout_ms + self.v12.round_finalize_grace_ms:
            errors.append("v12.round_timeout_ms must cover cloud-now timeout plus finalize grace")
        if self.v12.diagnosis_window_ms not in {50, 100, 150}:
            errors.append("v12.diagnosis_window_ms must be one of 50, 100, or 150")
        if self.v12.diagnosis_step_ms != self.v12.diagnosis_window_ms:
            errors.append("v12.diagnosis_step_ms must equal diagnosis_window_ms")
        if self.v12.diagnosis_overlap_enabled:
            errors.append("v12.diagnosis_overlap_enabled must be false")
        raw_capture = self.raw_sample_capture
        if raw_capture.enabled and not str(raw_capture.directory).strip():
            errors.append("raw_sample_capture.directory must be non-empty")
        if raw_capture.history_window_ms <= 0 or raw_capture.normal_sample_interval_seconds <= 0:
            errors.append("raw sample capture intervals must be positive")
        if raw_capture.local_retention_hours <= 0 or raw_capture.max_local_storage_mb <= 0:
            errors.append("raw sample capture retention and storage limits must be positive")
        if raw_capture.upload_batch_size <= 0:
            errors.append("raw_sample_capture.upload_batch_size must be positive")
        return errors
