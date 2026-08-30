from __future__ import annotations

import json
import re
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from compatibility.bearing_v12 import resolve_unit_id


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, init=False)
class SenderNodeConfig:
    sender_id: str
    unit_id: str
    scheduler_url: str
    mqtt_host: str
    mqtt_port: int
    # 阶段 4：目标 Edge 节点 ID → MQTT 代理端口映射（网络模拟按链路分端口）。
    # 为空时所有目标都使用 mqtt_port。
    edge_mqtt_proxy_ports: Mapping[str, int] = field(default_factory=dict)

    def __init__(
        self,
        sender_id: str,
        unit_id: str | None = None,
        scheduler_url: str = "",
        mqtt_host: str = "",
        mqtt_port: int = 0,
        edge_mqtt_proxy_ports: Mapping[str, int] | None = None,
        *,
        bearing_id: str | None = None,
    ) -> None:
        if unit_id is None and bearing_id is None:
            raise ValueError("unit_id or bearing_id is required")
        if unit_id is not None and bearing_id is not None and unit_id != bearing_id:
            raise ValueError("unit_id and bearing_id must match")
        resolved = unit_id if unit_id is not None else bearing_id
        assert resolved is not None
        object.__setattr__(self, "sender_id", sender_id)
        object.__setattr__(self, "unit_id", resolved)
        object.__setattr__(self, "scheduler_url", scheduler_url)
        object.__setattr__(self, "mqtt_host", mqtt_host)
        object.__setattr__(self, "mqtt_port", mqtt_port)
        object.__setattr__(
            self,
            "edge_mqtt_proxy_ports",
            edge_mqtt_proxy_ports or {},
        )

    @property
    def bearing_id(self) -> str:
        """Legacy bearing identity view retained for external contracts."""

        return self.unit_id


@dataclass(frozen=True)
class SenderConfig:
    device_id: str
    senders: tuple[SenderNodeConfig, ...]
    scheduler_timeout_seconds: float
    schedule_max_retries: int
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
    recovery_window_seconds: float = 60.0
    deferred_retry_window_seconds: float = 60.0
    deferred_retry_initial_seconds: float = 2.0
    deferred_retry_max_seconds: float = 16.0
    deferred_retry_jitter_ratio: float = 0.2


REQUIRED_FIELDS = tuple(
    name
    for name, member in SenderConfig.__dataclass_fields__.items()
    if member.default is MISSING and member.default_factory is MISSING
)
SENDER_REQUIRED_FIELDS = tuple(
    name
    for name, member in SenderNodeConfig.__dataclass_fields__.items()
    if name != "unit_id"
    and member.default is MISSING
    and member.default_factory is MISSING
)


def _non_empty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _load_sender(raw: Any, index: int) -> SenderNodeConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"senders[{index}] must be an object")
    missing = [field for field in SENDER_REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ConfigError(f"senders[{index}] missing fields: {', '.join(missing)}")

    sender_id = _non_empty_text(raw["sender_id"], f"senders[{index}].sender_id")
    try:
        unit_id = resolve_unit_id(raw, f"senders[{index}]")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    scheduler_url = _non_empty_text(raw["scheduler_url"], f"senders[{index}].scheduler_url")
    mqtt_host = _non_empty_text(raw["mqtt_host"], f"senders[{index}].mqtt_host")
    if not scheduler_url.startswith(("http://", "https://")):
        raise ConfigError(f"senders[{index}].scheduler_url must start with http:// or https://")
    try:
        mqtt_port = int(raw["mqtt_port"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"senders[{index}].mqtt_port must be an integer") from exc
    if not 1 <= mqtt_port <= 65535:
        raise ConfigError(f"senders[{index}].mqtt_port is outside the valid range")

    proxy_ports_raw = raw.get("edge_mqtt_proxy_ports", {})
    if not isinstance(proxy_ports_raw, dict):
        raise ConfigError(f"senders[{index}].edge_mqtt_proxy_ports must be an object")
    edge_mqtt_proxy_ports: dict[str, int] = {}
    for edge_id, port in proxy_ports_raw.items():
        if not isinstance(edge_id, str) or not edge_id.strip():
            raise ConfigError(
                f"senders[{index}].edge_mqtt_proxy_ports keys must be non-empty strings"
            )
        try:
            proxy_port = int(port)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"senders[{index}].edge_mqtt_proxy_ports.{edge_id} must be an integer"
            ) from exc
        if not 1 <= proxy_port <= 65535:
            raise ConfigError(
                f"senders[{index}].edge_mqtt_proxy_ports.{edge_id} is outside the valid range"
            )
        edge_mqtt_proxy_ports[edge_id] = proxy_port

    return SenderNodeConfig(
        sender_id=sender_id,
        unit_id=unit_id,
        scheduler_url=scheduler_url,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        edge_mqtt_proxy_ports=edge_mqtt_proxy_ports,
    )


def load_config(path: Path | str) -> SenderConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc

    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ConfigError(f"missing config fields: {', '.join(missing)}")
    raw_senders = raw["senders"]
    if not isinstance(raw_senders, list) or not raw_senders:
        raise ConfigError("senders must contain at least one sender configuration")
    senders = tuple(_load_sender(item, index) for index, item in enumerate(raw_senders))
    sender_ids = [item.sender_id for item in senders]
    unit_ids = [item.unit_id for item in senders]
    if len(set(sender_ids)) != len(sender_ids):
        raise ConfigError("sender_id values must be unique")
    if len(set(unit_ids)) != len(unit_ids):
        legacy_only = all(
            "bearing_id" in item and "unit_id" not in item
            for item in raw_senders
        )
        field = "bearing_id" if legacy_only else "unit_id"
        raise ConfigError(f"{field} values must be unique")

    try:
        config = SenderConfig(
            device_id=_non_empty_text(raw["device_id"], "device_id"),
            senders=senders,
            scheduler_timeout_seconds=float(raw["scheduler_timeout_seconds"]),
            schedule_max_retries=int(raw["schedule_max_retries"]),
            mqtt_keepalive_seconds=int(raw["mqtt_keepalive_seconds"]),
            qos=int(raw["qos"]),
            retain=bool(raw["retain"]),
            puback_warning_timeout_ms=int(raw["puback_warning_timeout_ms"]),
            packet_delivery_timeout_ms=int(raw["packet_delivery_timeout_ms"]),
            max_publish_retries=int(raw["max_publish_retries"]),
            recovery_window_seconds=float(raw.get("recovery_window_seconds", 60.0)),
            deferred_retry_window_seconds=float(
                raw.get("deferred_retry_window_seconds", 60.0)
            ),
            deferred_retry_initial_seconds=float(
                raw.get("deferred_retry_initial_seconds", 2.0)
            ),
            deferred_retry_max_seconds=float(
                raw.get("deferred_retry_max_seconds", 16.0)
            ),
            deferred_retry_jitter_ratio=float(
                raw.get("deferred_retry_jitter_ratio", 0.2)
            ),
            pending_queue_max_packets=int(raw["pending_queue_max_packets"]),
            task_duration_ms=int(raw["task_duration_ms"]),
            packet_interval_ms=int(raw["packet_interval_ms"]),
            expected_packet_count=int(raw["expected_packet_count"]),
            log_dir=(config_path.parent / raw["log_dir"]).resolve(),
            state_dir=(config_path.parent / raw["state_dir"]).resolve(),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid config value: {exc}") from exc

    if re.fullmatch(r".*\d+", config.device_id) is None:
        raise ConfigError("device_id must end with a numeric suffix")
    if config.qos != 1 or config.retain:
        raise ConfigError("current contract requires qos=1 and retain=false")
    if config.schedule_max_retries < 0 or config.max_publish_retries < 0:
        raise ConfigError("retry counts cannot be negative")
    if config.scheduler_timeout_seconds <= 0 or config.mqtt_keepalive_seconds <= 0:
        raise ConfigError("scheduler timeout and MQTT keepalive must be positive")
    if config.puback_warning_timeout_ms <= 0:
        raise ConfigError("puback warning timeout must be positive")
    if config.packet_delivery_timeout_ms <= config.puback_warning_timeout_ms:
        raise ConfigError("delivery timeout must exceed PUBACK warning timeout")
    if config.recovery_window_seconds <= 0:
        raise ConfigError("recovery window must be positive")
    if config.deferred_retry_window_seconds <= 0:
        raise ConfigError("deferred retry window must be positive")
    if config.deferred_retry_initial_seconds <= 0:
        raise ConfigError("deferred retry initial delay must be positive")
    if config.deferred_retry_max_seconds < config.deferred_retry_initial_seconds:
        raise ConfigError("deferred retry max delay must not be shorter than initial delay")
    if not 0 <= config.deferred_retry_jitter_ratio < 1:
        raise ConfigError("deferred retry jitter ratio must be between 0 and 1")
    if config.packet_interval_ms <= 0 or config.task_duration_ms <= 0:
        raise ConfigError("task and packet durations must be positive")
    if (
        config.packet_interval_ms != 50
        or config.expected_packet_count != 80
        or config.task_duration_ms != 4000
    ):
        raise ConfigError("current contract requires 50 ms windows, 80 packets, and 4000 ms tasks")
    expected = config.task_duration_ms // config.packet_interval_ms
    if config.task_duration_ms % config.packet_interval_ms or expected != config.expected_packet_count:
        raise ConfigError("expected_packet_count must match task duration / packet interval")
    if config.pending_queue_max_packets <= 0:
        raise ConfigError("pending queue size must be positive")

    return config
