# -*- coding: utf-8 -*-
"""边缘模型运行模块配置。

当前超时和队列数值只用于逐包技术闭环，不是 200 ms 端到端目标下的生产值。
生产值必须在 vLLM 逐包压测和整链路时延分配后重新冻结。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class QueueConfig:
    max_waiting_requests: int = 1
    full_policy: str = "reject"  # reject | replace


@dataclass
class TimeoutConfig:
    queue_wait_ms: int = 250
    inference_ms: int = 1500
    total_ms: int = 2000
    fallback_reserve_ms: int = 50


@dataclass
class BreakerConfig:
    enabled: bool = True
    consecutive_failure_threshold: int = 5
    recovery_probe_interval_s: float = 60.0
    probe_inference_timeout_ms: int = 2000


@dataclass
class ModelClientConfig:
    base_url: str = "http://127.0.0.1:8001"
    infer_path: str = "/infer"
    health_path: str = "/health"
    readiness_path: str = "/readiness"
    connect_timeout_ms: int = 500
    read_timeout_ms: int = 1500
    # 阶段 7.2：模型版本 pin；设置后 readiness 校验服务端版本，不一致视为未就绪。
    expected_version: Optional[str] = None
    # 阶段 7.4：后台就绪探针周期（秒）。
    readiness_probe_interval_s: float = 5.0


@dataclass
class FallbackConfig:
    # 阶段 6：正式模型路线的降级语义是"诊断不可用"（不再回退旧模型）。
    rule_version: str = "diagnosis_unavailable_v1"
    allow_test_rule: bool = True


@dataclass
class EdgeModelConfig:
    diagnostic_backend: str = "local"
    queue: QueueConfig = field(default_factory=QueueConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    model_client: ModelClientConfig = field(default_factory=ModelClientConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.diagnostic_backend not in {"local", "http", "local_h5"}:
            errors.append("diagnostic_backend must be local, http or local_h5")
        if self.queue.max_waiting_requests < 1:
            errors.append("queue.max_waiting_requests 必须 >= 1")
        if self.queue.full_policy not in ("reject", "replace"):
            errors.append("queue.full_policy 必须是 reject 或 replace")
        if not (0 < self.timeout.queue_wait_ms < self.timeout.total_ms):
            errors.append("timeout.queue_wait_ms 必须为正数且小于 total_ms")
        if not (0 < self.timeout.inference_ms <= self.timeout.total_ms):
            errors.append("timeout.inference_ms 必须为正数且不超过 total_ms")
        if not (0 < self.timeout.fallback_reserve_ms < self.timeout.total_ms):
            errors.append("timeout.fallback_reserve_ms 必须为正数且小于 total_ms")
        if not (self.timeout.queue_wait_ms + self.timeout.inference_ms
                + self.timeout.fallback_reserve_ms <= self.timeout.total_ms):
            errors.append("超时关系不成立: queue_wait + inference + fallback_reserve 必须 <= total_ms")
        if self.breaker.consecutive_failure_threshold < 1:
            errors.append("breaker.consecutive_failure_threshold 必须 >= 1")
        if self.breaker.recovery_probe_interval_s <= 0:
            errors.append("breaker.recovery_probe_interval_s 必须为正数")
        if self.model_client.readiness_probe_interval_s <= 0:
            errors.append("model_client.readiness_probe_interval_s 必须为正数")
        if not self.fallback.rule_version:
            errors.append("fallback.rule_version 不能为空")
        if self.fallback.rule_version.startswith("edge_rule_test") and not self.fallback.allow_test_rule:
            errors.append("fallback.rule_version 是测试规则但 fallback.allow_test_rule 未开启")
        return errors

    def as_dict(self) -> Dict:
        return {
            "diagnostic_backend": self.diagnostic_backend,
            "queue": _asdict(self.queue),
            "timeout": _asdict(self.timeout),
            "breaker": _asdict(self.breaker),
            "model_client": _asdict(self.model_client),
            "fallback": _asdict(self.fallback),
        }


def _asdict(obj) -> Dict:
    import dataclasses
    return dataclasses.asdict(obj)
