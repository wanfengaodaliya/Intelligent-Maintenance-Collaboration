# -*- coding: utf-8 -*-
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


LOAD_STATUSES = {"LOADING", "LOADED", "UNLOADED", "ERROR"}
NETWORK_MEASUREMENT_STATUSES = {"OK", "STALE", "DISCONNECTED", "FAILED"}
QUEUE_MEASUREMENT_STATUSES = {"OK", "STALE", "FAILED"}
CPU_MEASUREMENT_STATUSES = {"OK", "DEGRADED", "FAILED"}
ACCELERATOR_MEASUREMENT_STATUSES = {"OK", "STALE", "FAILED"}
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


def _non_empty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _integer(value: object, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{field} 超出允许范围")
    return value


def _finite_number(value: object, field: str, *, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数值")
    number = float(value)
    if not math.isfinite(number) or number < minimum or maximum is not None and number > maximum:
        raise ValueError(f"{field} 超出允许范围")
    return number


@dataclass(frozen=True)
class ModelStatus:
    model_version: str
    load_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_version", _non_empty_text(self.model_version, "model_version"))
        normalized = _non_empty_text(self.load_status, "load_status").upper()
        if normalized not in LOAD_STATUSES:
            raise ValueError("load_status 不受支持")
        object.__setattr__(self, "load_status", normalized)

    def as_dict(self) -> dict[str, str]:
        return {"model_version": self.model_version, "load_status": self.load_status}


@dataclass(frozen=True)
class BusinessStatusSnapshot:
    edge_node_id: str
    queue_length: int
    models: tuple[ModelStatus, ...]
    last_task_activity_ns: int
    queue_measurement_status: str = "OK"
    queue_breakdown: dict[str, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_node_id", _non_empty_text(self.edge_node_id, "edge_node_id"))
        _integer(self.queue_length, "queue_length", minimum=0)
        if not isinstance(self.models, tuple) or not all(isinstance(item, ModelStatus) for item in self.models):
            raise ValueError("models 必须是 ModelStatus 元组")
        _integer(self.last_task_activity_ns, "last_task_activity_ns", minimum=0, maximum=SQLITE_MAX_INTEGER)
        queue_status = _non_empty_text(self.queue_measurement_status, "queue_measurement_status").upper()
        if queue_status not in QUEUE_MEASUREMENT_STATUSES:
            raise ValueError("queue_measurement_status 不受支持")
        object.__setattr__(self, "queue_measurement_status", queue_status)
        if self.queue_breakdown is not None:
            if not isinstance(self.queue_breakdown, dict):
                raise ValueError("queue_breakdown 必须是字典")
            for name, value in self.queue_breakdown.items():
                _non_empty_text(name, "queue_breakdown key")
                _integer(value, f"queue_breakdown.{name}", minimum=0)


@dataclass(frozen=True)
class ResourceSnapshot:
    logical_cpu_count: int
    cpu_utilization_percent: float
    memory_available_mb: float
    cpu_measurement_status: str = "OK"
    memory_measurement_status: str = "OK"

    def __post_init__(self) -> None:
        _integer(self.logical_cpu_count, "logical_cpu_count", minimum=1)
        object.__setattr__(self, "cpu_utilization_percent", _finite_number(self.cpu_utilization_percent, "cpu_utilization_percent", minimum=0.0, maximum=100.0))
        object.__setattr__(self, "memory_available_mb", _finite_number(self.memory_available_mb, "memory_available_mb", minimum=0.0))
        cpu_status = _non_empty_text(self.cpu_measurement_status, "cpu_measurement_status").upper()
        if cpu_status not in CPU_MEASUREMENT_STATUSES:
            raise ValueError("cpu_measurement_status 不受支持")
        object.__setattr__(self, "cpu_measurement_status", cpu_status)
        memory_status = _non_empty_text(self.memory_measurement_status, "memory_measurement_status").upper()
        if memory_status not in CPU_MEASUREMENT_STATUSES:
            raise ValueError("memory_measurement_status 不受支持")
        object.__setattr__(self, "memory_measurement_status", memory_status)


@dataclass(frozen=True)
class AcceleratorSnapshot:
    gpu_available: bool
    npu_available: bool
    measurement_status: str = "OK"

    def __post_init__(self) -> None:
        if not isinstance(self.gpu_available, bool) or not isinstance(self.npu_available, bool):
            raise ValueError("gpu_available 和 npu_available 必须是布尔值")
        accelerator_status = _non_empty_text(self.measurement_status, "measurement_status").upper()
        if accelerator_status not in ACCELERATOR_MEASUREMENT_STATUSES:
            raise ValueError("measurement_status 不受支持")
        object.__setattr__(self, "measurement_status", accelerator_status)


@dataclass(frozen=True)
class NetworkSnapshot:
    """Edge→Scheduler 网络快照。

    measured_at_ns 语义随 measurement_status 变化（EDGE-5）：
    - OK：本次快照产出的“当前有效测量时间”；
    - 非 OK（STALE / DISCONNECTED / FAILED）：本次失败检测 / 快照产生时间，
      不是“成功测量时间”。
    可信的最近一次成功测量时间请使用 last_successful_measurement_ns。

    网络字段真实性（NET-4）：
    - ``loss_rate`` 来源是 Network Simulator 的模拟 packet loss，不是 MQTT 实测
      发布丢包率，不得换算为“发布成功率”。
    - ``rtt_ms_p95`` 是估算值（latency + 2*jitter 一类推算，见 ``rtt_p95_is_estimate``），
      不是由大量真实 RTT 样本计算出的测量 P95。
    """

    measured_at_ns: int
    available_uplink_mbps_estimate: float
    rtt_ms_avg: float
    rtt_ms_p95: float
    loss_rate: float
    measurement_status: str = "OK"
    rtt_p95_is_estimate: bool = True
    last_successful_measurement_ns: int | None = None

    def __post_init__(self) -> None:
        _integer(self.measured_at_ns, "measured_at_ns", minimum=1, maximum=SQLITE_MAX_INTEGER)
        object.__setattr__(self, "available_uplink_mbps_estimate", _finite_number(self.available_uplink_mbps_estimate, "available_uplink_mbps_estimate", minimum=0.0))
        object.__setattr__(self, "rtt_ms_avg", _finite_number(self.rtt_ms_avg, "rtt_ms_avg", minimum=0.0))
        object.__setattr__(self, "rtt_ms_p95", _finite_number(self.rtt_ms_p95, "rtt_ms_p95", minimum=0.0))
        object.__setattr__(self, "loss_rate", _finite_number(self.loss_rate, "loss_rate", minimum=0.0, maximum=1.0))
        status = _non_empty_text(self.measurement_status, "measurement_status").upper()
        if status not in NETWORK_MEASUREMENT_STATUSES:
            raise ValueError("measurement_status 不受支持")
        object.__setattr__(self, "measurement_status", status)
        if not isinstance(self.rtt_p95_is_estimate, bool):
            raise ValueError("rtt_p95_is_estimate 必须是布尔值")
        if self.last_successful_measurement_ns is not None:
            _integer(self.last_successful_measurement_ns, "last_successful_measurement_ns", minimum=1, maximum=SQLITE_MAX_INTEGER)

    def as_dict(self) -> dict[str, int | float | str | None]:
        return {
            "measured_at_ns": self.measured_at_ns,
            "available_uplink_mbps_estimate": self.available_uplink_mbps_estimate,
            "rtt_ms_avg": self.rtt_ms_avg,
            "rtt_ms_p95": self.rtt_ms_p95,
            "loss_rate": self.loss_rate,
            "measurement_status": self.measurement_status,
            "rtt_p95_is_estimate": self.rtt_p95_is_estimate,
            "last_successful_measurement_ns": self.last_successful_measurement_ns,
        }


@dataclass(frozen=True)
class EdgeStatusReport:
    edge_node_id: str
    reported_at_ns: int
    resources: ResourceSnapshot
    accelerators: AcceleratorSnapshot
    network_to_scheduler: NetworkSnapshot
    queue_length: int
    models: tuple[ModelStatus, ...]
    last_task_activity_ns: int
    queue_measurement_status: str = "OK"
    queue_breakdown: dict[str, int] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_node_id", _non_empty_text(self.edge_node_id, "edge_node_id"))
        _integer(self.reported_at_ns, "reported_at_ns", minimum=1, maximum=SQLITE_MAX_INTEGER)
        if not isinstance(self.resources, ResourceSnapshot):
            raise ValueError("resources 必须是 ResourceSnapshot")
        if not isinstance(self.accelerators, AcceleratorSnapshot):
            raise ValueError("accelerators 必须是 AcceleratorSnapshot")
        if not isinstance(self.network_to_scheduler, NetworkSnapshot):
            raise ValueError("network_to_scheduler must be NetworkSnapshot")
        _integer(self.queue_length, "queue_length", minimum=0)
        if not isinstance(self.models, tuple) or not all(isinstance(item, ModelStatus) for item in self.models):
            raise ValueError("models 必须是 ModelStatus 元组")
        _integer(self.last_task_activity_ns, "last_task_activity_ns", minimum=0, maximum=SQLITE_MAX_INTEGER)
        queue_status = _non_empty_text(self.queue_measurement_status, "queue_measurement_status").upper()
        if queue_status not in QUEUE_MEASUREMENT_STATUSES:
            raise ValueError("queue_measurement_status 不受支持")
        object.__setattr__(self, "queue_measurement_status", queue_status)
        if self.queue_breakdown is not None:
            if not isinstance(self.queue_breakdown, dict):
                raise ValueError("queue_breakdown 必须是字典")
            for name, value in self.queue_breakdown.items():
                _non_empty_text(name, "queue_breakdown key")
                _integer(value, f"queue_breakdown.{name}", minimum=0)

    def as_dict(self) -> dict[str, Any]:
        resources: dict[str, Any] = {
            "logical_cpu_count": self.resources.logical_cpu_count,
            "cpu_utilization_percent": self.resources.cpu_utilization_percent,
            "memory_available_mb": self.resources.memory_available_mb,
            "cpu_measurement_status": self.resources.cpu_measurement_status,
            "memory_measurement_status": self.resources.memory_measurement_status,
            "gpu_available": self.accelerators.gpu_available,
            "npu_available": self.accelerators.npu_available,
            "accelerator_measurement_status": self.accelerators.measurement_status,
            "queue_length": self.queue_length,
            "queue_measurement_status": self.queue_measurement_status,
        }
        if self.queue_breakdown is not None:
            resources["queue_breakdown"] = dict(self.queue_breakdown)
        return {
            "edge_node_id": self.edge_node_id,
            "reported_at_ns": self.reported_at_ns,
            "resources": resources,
            "models": [item.as_dict() for item in self.models],
            "network_to_scheduler": self.network_to_scheduler.as_dict(),
            "last_task_activity_ns": self.last_task_activity_ns,
        }
