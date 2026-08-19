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
    suggestion_topic: str = "summary/suggestions"
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
class V12RuntimeConfig:
    enabled: bool = True
    database_path: Path = Path("data/edge_v12.db")
    cloud_now_timeout_ms: int = 3_000
    round_finalize_grace_ms: int = 500
    round_timeout_ms: int = 3_500
    diagnosis_window_ms: int = 50
    diagnosis_step_ms: int = 50
    diagnosis_overlap_enabled: bool = False
    http_connect_timeout_ms: int = 500
    http_read_timeout_ms: int = 2_000
    late_correction_retention_ms: int = 3_600_000
    device_result_publish_max_attempts: int = 5
    # 建议发布重试上限，超过后进入死信等待人工恢复。
    suggestion_publish_max_attempts: int = 5
    # 阶段 5：结果上报重试上限，超过后进入死信等待人工恢复。
    result_upload_max_attempts: int = 8
    # 阶段 5：已发布 Outbox 记录的保留期（小时），0 表示禁用自动清理。
    outbox_published_retention_hours: int = 168


@dataclass(frozen=True)
class MaintenanceConfig:
    enabled: bool = True
    interval_seconds: float = 0.5


@dataclass(frozen=True)
class RawSampleCaptureConfig:
    enabled: bool = True
    directory: Path = Path("data/raw_analysis_samples")
    history_window_ms: int = 1_000
    normal_sample_interval_seconds: int = 60
    local_retention_hours: int = 24
    max_local_storage_mb: int = 2_048
    upload_batch_size: int = 1
    # 阶段 5：原始样本上传重试上限，超过后进入死信等待人工恢复。
    max_upload_attempts: int = 10


@dataclass(frozen=True)
class SuggestionLlmConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8002"
    timeout_seconds: float = 3.0
    history_window: int = 10
    fallback_text: str = "设备异常，建议关注。"


@dataclass(frozen=True)
class EdgeRuntimeConfig:
    edge_node_id: str = "edge_01"
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    control: ControlServerConfig = field(default_factory=ControlServerConfig)
    v12: V12RuntimeConfig = field(default_factory=V12RuntimeConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    suggestion_llm: SuggestionLlmConfig = field(default_factory=SuggestionLlmConfig)
    raw_sample_capture: RawSampleCaptureConfig = field(default_factory=RawSampleCaptureConfig)
    cloud_node_urls: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EdgeRuntimeConfig":
        env = os.environ if environ is None else environ
        edge_node_id = env.get("EDGE_NODE_ID", "edge_01").strip()
        cloud_node_urls = json.loads(env.get("EDGE_CLOUD_NODE_URLS_JSON", "{}"))
        if not isinstance(cloud_node_urls, dict):
            raise ValueError("EDGE_CLOUD_NODE_URLS_JSON must be a JSON object")
        if not cloud_node_urls:
            cloud_node_urls = {
                "cloud_01": env.get(
                    "CLOUD_SERVICE_BASE_URL", "http://127.0.0.1:18021"
                ).rstrip("/")
            }
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
                suggestion_topic=env.get(
                    "EDGE_MQTT_SUGGESTION_TOPIC",
                    "summary/suggestions",
                ).strip(),
                client_id=env.get(
                    "EDGE_MQTT_CLIENT_ID",
                    f"{edge_node_id}-runtime",
                ).strip(),
            ),
            suggestion_llm=SuggestionLlmConfig(
                enabled=env.get("EDGE_SUGGESTION_LLM_ENABLED", "true").strip().lower() == "true",
                base_url=env.get(
                    "EDGE_SUGGESTION_LLM_BASE_URL",
                    "http://127.0.0.1:8002",
                ).rstrip("/"),
                timeout_seconds=float(
                    env.get("EDGE_SUGGESTION_LLM_TIMEOUT_SECONDS", "3.0")
                ),
                history_window=int(
                    env.get("EDGE_SUGGESTION_HISTORY_WINDOW", "10")
                ),
                fallback_text=env.get(
                    "EDGE_SUGGESTION_FALLBACK_TEXT",
                    "设备异常，建议关注。",
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
                cloud_now_timeout_ms=int(env.get("EDGE_CLOUD_NOW_TIMEOUT_MS", "3000")),
                round_finalize_grace_ms=int(env.get("EDGE_ROUND_FINALIZE_GRACE_MS", "500")),
                round_timeout_ms=int(env.get("EDGE_ROUND_TIMEOUT_MS", "3500")),
                diagnosis_window_ms=int(env.get("EDGE_DIAGNOSIS_WINDOW_MS", "50")),
                diagnosis_step_ms=int(env.get("EDGE_DIAGNOSIS_STEP_MS", "50")),
                diagnosis_overlap_enabled=env.get("EDGE_DIAGNOSIS_OVERLAP_ENABLED", "false").strip().lower() == "true",
                http_connect_timeout_ms=int(env.get("EDGE_HTTP_CONNECT_TIMEOUT_MS", "500")),
                http_read_timeout_ms=int(env.get("EDGE_HTTP_READ_TIMEOUT_MS", "2000")),
                late_correction_retention_ms=int(
                    env.get("EDGE_LATE_CORRECTION_RETENTION_MS", "3600000")
                ),
                device_result_publish_max_attempts=int(
                    env.get("EDGE_DEVICE_RESULT_PUBLISH_MAX_ATTEMPTS", "5")
                ),
                suggestion_publish_max_attempts=int(
                    env.get("EDGE_SUGGESTION_PUBLISH_MAX_ATTEMPTS", "5")
                ),
                result_upload_max_attempts=int(
                    env.get("EDGE_RESULT_UPLOAD_MAX_ATTEMPTS", "8")
                ),
                outbox_published_retention_hours=int(
                    env.get("EDGE_OUTBOX_PUBLISHED_RETENTION_HOURS", "168")
                ),
            ),
            maintenance=MaintenanceConfig(
                enabled=env.get("EDGE_MAINTENANCE_ENABLED", "true").strip().lower() == "true",
                interval_seconds=float(env.get("EDGE_MAINTENANCE_INTERVAL_SECONDS", "0.5")),
            ),
            raw_sample_capture=RawSampleCaptureConfig(
                enabled=env.get("EDGE_RAW_SAMPLE_CAPTURE_ENABLED", "true").strip().lower() == "true",
                directory=Path(env.get("EDGE_RAW_SAMPLE_DIRECTORY", "data/raw_analysis_samples")),
                history_window_ms=int(env.get("EDGE_RAW_SAMPLE_HISTORY_WINDOW_MS", "1000")),
                normal_sample_interval_seconds=int(env.get("EDGE_RAW_SAMPLE_NORMAL_INTERVAL_SECONDS", "60")),
                local_retention_hours=int(env.get("EDGE_RAW_SAMPLE_RETENTION_HOURS", "24")),
                max_local_storage_mb=int(env.get("EDGE_RAW_SAMPLE_MAX_STORAGE_MB", "2048")),
                upload_batch_size=int(env.get("EDGE_RAW_SAMPLE_UPLOAD_BATCH_SIZE", "1")),
                max_upload_attempts=int(env.get("EDGE_RAW_SAMPLE_MAX_UPLOAD_ATTEMPTS", "10")),
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
        if self.v12.enabled and not str(self.v12.database_path).strip():
            errors.append("v12.database_path must be non-empty")
        if self.v12.cloud_now_timeout_ms <= 0 or self.v12.round_finalize_grace_ms < 0:
            errors.append("v12 cloud-now timeout and finalize grace are invalid")
        if self.v12.round_timeout_ms < self.v12.cloud_now_timeout_ms + self.v12.round_finalize_grace_ms:
            errors.append("v12.round_timeout_ms must cover cloud-now timeout plus finalize grace")
        if self.v12.http_connect_timeout_ms <= 0 or self.v12.http_read_timeout_ms <= 0:
            errors.append("v12 HTTP connect and read timeouts must be positive")
        elif self.v12.http_connect_timeout_ms >= self.v12.http_read_timeout_ms:
            errors.append("v12.http_connect_timeout_ms must be shorter than http_read_timeout_ms")
        elif (
            self.v12.http_connect_timeout_ms + self.v12.http_read_timeout_ms
            >= self.v12.cloud_now_timeout_ms
        ):
            errors.append(
                "v12 single HTTP request budget must stay below cloud_now_timeout_ms"
            )
        if self.v12.late_correction_retention_ms <= 0:
            errors.append("v12.late_correction_retention_ms must be positive")
        if self.v12.device_result_publish_max_attempts <= 0:
            errors.append("v12.device_result_publish_max_attempts must be positive")
        if self.v12.suggestion_publish_max_attempts <= 0:
            errors.append("v12.suggestion_publish_max_attempts must be positive")
        if self.v12.result_upload_max_attempts <= 0:
            errors.append("v12.result_upload_max_attempts must be positive")
        if self.v12.outbox_published_retention_hours < 0:
            errors.append("v12.outbox_published_retention_hours must not be negative")
        if not 0.1 <= self.maintenance.interval_seconds <= 10.0:
            errors.append("maintenance.interval_seconds must be within [0.1, 10.0]")
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
        if raw_capture.max_upload_attempts <= 0:
            errors.append("raw_sample_capture.max_upload_attempts must be positive")
        suggestion = self.suggestion_llm
        if suggestion.enabled:
            if not suggestion.base_url.startswith(("http://", "https://")):
                errors.append("suggestion_llm.base_url must use HTTP or HTTPS")
            if suggestion.timeout_seconds <= 0:
                errors.append("suggestion_llm.timeout_seconds must be positive")
            if suggestion.history_window < 1:
                errors.append("suggestion_llm.history_window must be at least 1")
        return errors
