# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _boolean(raw: str, field: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{field} 必须是布尔值")


def _optional_boolean(raw: str | None, field: str) -> bool | None:
    if raw is None or not raw.strip():
        return None
    return _boolean(raw, field)


def _positive_float(raw: str, field: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是正数") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} 必须是正数")
    return value


def _positive_int(raw: str, field: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是正整数") from exc
    if value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _non_negative_int(raw: str, field: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是非负整数") from exc
    if value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _http_url(value: str, field: str) -> str:
    if any(character.isspace() for character in value):
        raise ValueError(f"{field} 必须是 HTTP(S) 完整地址")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} 必须是 HTTP(S) 完整地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 HTTP(S) 完整地址") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{field} 必须是 HTTP(S) 完整地址")
    return value


@dataclass(frozen=True)
class StatusTargetConfig:
    name: str
    enabled: bool
    url: str
    timeout_seconds: float
    retry_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("target.name 必须是非空字符串")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"{self.name}.enabled 必须是布尔值")
        if self.enabled:
            _http_url(self.url, f"{self.name}.url")
            if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0 or not math.isfinite(float(self.timeout_seconds)):
                raise ValueError(f"{self.name}.timeout_seconds 必须是正数")
            if isinstance(self.retry_count, bool) or not isinstance(self.retry_count, int) or self.retry_count < 0:
                raise ValueError(f"{self.name}.retry_count 必须是非负整数")


@dataclass(frozen=True)
class ResourceConfig:
    mode: str = "system"
    logical_cpu_count: int | None = None
    memory_limit_mb: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"system", "process"}:
            raise ValueError("resource.mode 必须是 system 或 process")
        if self.mode == "process":
            if isinstance(self.logical_cpu_count, bool) or not isinstance(self.logical_cpu_count, int) or self.logical_cpu_count <= 0:
                raise ValueError("process 模式必须配置正整数 logical_cpu_count")
            if isinstance(self.memory_limit_mb, bool) or not isinstance(self.memory_limit_mb, (int, float)) or not math.isfinite(float(self.memory_limit_mb)) or self.memory_limit_mb <= 0:
                raise ValueError("process 模式必须配置正数 memory_limit_mb")


@dataclass(frozen=True)
class AcceleratorConfig:
    gpu_available_override: bool | None = None
    npu_available_override: bool | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("gpu_available_override", self.gpu_available_override),
            ("npu_available_override", self.npu_available_override),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field} 必须是布尔值或 None")


@dataclass(frozen=True)
class NetworkConfig:
    url: str
    timeout_seconds: float = 0.5
    stale_after_seconds: float = 3.0

    def __post_init__(self) -> None:
        _http_url(self.url, "network.url")
        if self.timeout_seconds <= 0 or not math.isfinite(float(self.timeout_seconds)):
            raise ValueError("network.timeout_seconds is invalid")
        if self.stale_after_seconds < 0 or not math.isfinite(float(self.stale_after_seconds)):
            raise ValueError("network.stale_after_seconds is invalid")


@dataclass(frozen=True)
class EdgeStatusReporterConfig:
    enabled: bool
    interval_seconds: float
    model_version: str
    scheduler: StatusTargetConfig
    cloud: StatusTargetConfig
    resource: ResourceConfig
    accelerator: AcceleratorConfig
    network: NetworkConfig

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        if not self.enabled:
            return
        if isinstance(self.interval_seconds, bool) or not isinstance(self.interval_seconds, (int, float)) or self.interval_seconds <= 0 or not math.isfinite(float(self.interval_seconds)):
            raise ValueError("interval_seconds 必须是正数")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("model_version 必须是非空字符串")
        if not self.scheduler.enabled and not self.cloud.enabled:
            raise ValueError("至少启用一个状态上报目标")

    @classmethod
    def from_env(
        cls,
        *,
        default_model_version: str,
        edge_node_id: str = "edge_01",
        environ: Mapping[str, str] | None = None,
    ) -> "EdgeStatusReporterConfig":
        env = os.environ if environ is None else environ
        network_link_id = env.get(
            "EDGE_NETWORK_LINK_ID",
            f"{edge_node_id}__to__scheduler__http",
        ).strip()
        default_network_url = (
            "http://127.0.0.1:8090/api/v1/network/links/"
            f"{network_link_id}"
        )
        scheduler_default_url, cloud_default_url = _default_status_urls(edge_node_id)
        enabled = _boolean(env.get("EDGE_STATUS_REPORTER_ENABLED", "true"), "EDGE_STATUS_REPORTER_ENABLED")
        if not enabled:
            return cls(
                enabled=False,
                interval_seconds=1.0,
                model_version=default_model_version,
                scheduler=StatusTargetConfig("scheduler", False, "", 0.5, 1),
                cloud=StatusTargetConfig("cloud", False, "", 0.5, 1),
                resource=ResourceConfig(),
                accelerator=AcceleratorConfig(),
                network=NetworkConfig(default_network_url),
            )
        if re.fullmatch(r"[A-Za-z0-9_-]+", network_link_id) is None:
            raise ValueError(
                "EDGE_NETWORK_LINK_ID may contain only letters, numbers, "
                "underscores, and hyphens"
            )
        scheduler_enabled = _boolean(
            env.get("EDGE_STATUS_SCHEDULER_ENABLED", "true"),
            "EDGE_STATUS_SCHEDULER_ENABLED",
        )
        scheduler = StatusTargetConfig(
            name="scheduler",
            enabled=scheduler_enabled,
            url=(
                env.get("EDGE_STATUS_SCHEDULER_URL", scheduler_default_url).strip()
                if scheduler_enabled else ""
            ),
            timeout_seconds=(
                _positive_float(env.get("EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS", "0.5"), "EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS")
                if scheduler_enabled else 0.5
            ),
            retry_count=(
                _non_negative_int(env.get("EDGE_STATUS_SCHEDULER_RETRY_COUNT", "1"), "EDGE_STATUS_SCHEDULER_RETRY_COUNT")
                if scheduler_enabled else 1
            ),
        )
        cloud_enabled = _boolean(
            env.get("EDGE_STATUS_CLOUD_ENABLED", "true"),
            "EDGE_STATUS_CLOUD_ENABLED",
        )
        cloud = StatusTargetConfig(
            name="cloud",
            enabled=cloud_enabled,
            url=(
                env.get("EDGE_STATUS_CLOUD_URL", cloud_default_url).strip()
                if cloud_enabled else ""
            ),
            timeout_seconds=(
                _positive_float(env.get("EDGE_STATUS_CLOUD_TIMEOUT_SECONDS", "0.5"), "EDGE_STATUS_CLOUD_TIMEOUT_SECONDS")
                if cloud_enabled else 0.5
            ),
            retry_count=(
                _non_negative_int(env.get("EDGE_STATUS_CLOUD_RETRY_COUNT", "1"), "EDGE_STATUS_CLOUD_RETRY_COUNT")
                if cloud_enabled else 1
            ),
        )
        mode = env.get("EDGE_STATUS_RESOURCE_MODE", "system").strip().lower()
        if mode == "process":
            resource = ResourceConfig(
                mode=mode,
                logical_cpu_count=_positive_int(env.get("EDGE_STATUS_PROCESS_LOGICAL_CPU_COUNT", ""), "EDGE_STATUS_PROCESS_LOGICAL_CPU_COUNT"),
                memory_limit_mb=_positive_float(env.get("EDGE_STATUS_PROCESS_MEMORY_LIMIT_MB", ""), "EDGE_STATUS_PROCESS_MEMORY_LIMIT_MB"),
            )
        else:
            resource = ResourceConfig(mode=mode)
        return cls(
            enabled=True,
            interval_seconds=_positive_float(env.get("EDGE_STATUS_INTERVAL_SECONDS", "1.0"), "EDGE_STATUS_INTERVAL_SECONDS"),
            model_version=env.get("EDGE_STATUS_MODEL_VERSION", default_model_version).strip(),
            scheduler=scheduler,
            cloud=cloud,
            resource=resource,
            accelerator=AcceleratorConfig(
                gpu_available_override=_optional_boolean(env.get("EDGE_STATUS_GPU_AVAILABLE_OVERRIDE"), "EDGE_STATUS_GPU_AVAILABLE_OVERRIDE"),
                npu_available_override=_optional_boolean(env.get("EDGE_STATUS_NPU_AVAILABLE_OVERRIDE"), "EDGE_STATUS_NPU_AVAILABLE_OVERRIDE"),
            ),
            network=NetworkConfig(
                url=env.get(
                    "EDGE_NETWORK_STATUS_URL",
                    default_network_url,
                ).strip(),
                timeout_seconds=_positive_float(
                    env.get("EDGE_NETWORK_STATUS_TIMEOUT_SECONDS", "0.5"),
                    "EDGE_NETWORK_STATUS_TIMEOUT_SECONDS",
                ),
                stale_after_seconds=float(env.get("EDGE_NETWORK_STATUS_STALE_SECONDS", "3.0")),
            ),
        )


def _default_status_urls(edge_node_id: str) -> tuple[str, str]:
    local_proxy_ports = {
        "edge_01": (18011, 18021),
        "edge_02": (18051, 18053),
    }
    ports = local_proxy_ports.get(edge_node_id)
    if ports is None:
        return (
            "http://127.0.0.1:8003/scheduler/edge-nodes/status",
            "http://127.0.0.1:8004/cloud/edge-status",
        )
    scheduler_port, cloud_port = ports
    return (
        f"http://127.0.0.1:{scheduler_port}/scheduler/edge-nodes/status",
        f"http://127.0.0.1:{cloud_port}/cloud/edge-status",
    )
