from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SenderConfig:
    sender_id: str
    scheduler_url: str
    scheduler_timeout_seconds: float
    schedule_max_retries: int
    mqtt_host: str
    mqtt_port: int
    mqtt_keepalive_seconds: int
    qos: int
    retain: bool
    puback_warning_timeout_ms: int
    packet_delivery_timeout_ms: int
    max_publish_retries: int
    pending_queue_max_packets: int
    task_duration_ms: int
    packet_interval_ms: int
    expected_packet_count: int
    log_dir: Path
    state_dir: Path


REQUIRED_FIELDS = tuple(SenderConfig.__dataclass_fields__)


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def load_config(path: Path | str) -> SenderConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc

    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ConfigError(f"missing config fields: {', '.join(missing)}")

    sender_id = _non_empty_text(raw["sender_id"], "sender_id")
    scheduler_url = _non_empty_text(raw["scheduler_url"], "scheduler_url")
    mqtt_host = _non_empty_text(raw["mqtt_host"], "mqtt_host")
    if not scheduler_url.startswith(("http://", "https://")):
        raise ConfigError("scheduler_url must start with http:// or https://")

    try:
        config = SenderConfig(
            sender_id=sender_id,
            scheduler_url=scheduler_url,
            scheduler_timeout_seconds=float(raw["scheduler_timeout_seconds"]),
            schedule_max_retries=int(raw["schedule_max_retries"]),
            mqtt_host=mqtt_host,
            mqtt_port=int(raw["mqtt_port"]),
            mqtt_keepalive_seconds=int(raw["mqtt_keepalive_seconds"]),
            qos=int(raw["qos"]),
            retain=bool(raw["retain"]),
            puback_warning_timeout_ms=int(raw["puback_warning_timeout_ms"]),
            packet_delivery_timeout_ms=int(raw["packet_delivery_timeout_ms"]),
            max_publish_retries=int(raw["max_publish_retries"]),
            pending_queue_max_packets=int(raw["pending_queue_max_packets"]),
            task_duration_ms=int(raw["task_duration_ms"]),
            packet_interval_ms=int(raw["packet_interval_ms"]),
            expected_packet_count=int(raw["expected_packet_count"]),
            log_dir=(config_path.parent / raw["log_dir"]).resolve(),
            state_dir=(config_path.parent / raw["state_dir"]).resolve(),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid config value: {exc}") from exc

    if config.qos != 1 or config.retain:
        raise ConfigError("current contract requires qos=1 and retain=false")
    if config.schedule_max_retries < 0 or config.max_publish_retries < 0:
        raise ConfigError("retry counts cannot be negative")
    if config.puback_warning_timeout_ms <= 0:
        raise ConfigError("puback warning timeout must be positive")
    if config.packet_delivery_timeout_ms <= config.puback_warning_timeout_ms:
        raise ConfigError("delivery timeout must exceed PUBACK warning timeout")
    if config.packet_interval_ms <= 0 or config.task_duration_ms <= 0:
        raise ConfigError("task and packet durations must be positive")
    expected = config.task_duration_ms // config.packet_interval_ms
    if config.task_duration_ms % config.packet_interval_ms or expected != config.expected_packet_count:
        raise ConfigError("expected_packet_count must match task duration / packet interval")
    if not 1 <= config.pending_queue_max_packets:
        raise ConfigError("pending queue size must be positive")
    if not 1 <= config.mqtt_port <= 65535:
        raise ConfigError("mqtt_port is outside the valid range")

    return config

