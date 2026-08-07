# -*- coding: utf-8 -*-
"""校验和原始缓存的显式启动配置。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


_RANGE_CHANNELS = {
    "vibration",
    "phase_current_1_A",
    "phase_current_2_A",
    "shaft_speed_rpm",
    "load_torque_nm",
    "bearing_radial_load_n",
    "bearing_module_temperature_c",
}


@dataclass(frozen=True)
class ValueRange:
    minimum: float
    maximum: float

    def valid(self) -> bool:
        return (
            _finite_number(self.minimum)
            and _finite_number(self.maximum)
            and float(self.minimum) <= float(self.maximum)
        )


@dataclass(frozen=True)
class ValidationCacheConfig:
    raw_cache_retention_seconds: float
    max_receive_rate_per_sender: float
    context_queue_capacity_per_sender: int
    raw_cache_capacity_per_sender: int
    context_before_packet_count: int
    cache_cleanup_interval_seconds: float
    hard_value_ranges: Mapping[str, ValueRange]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not _finite_number(self.raw_cache_retention_seconds) or self.raw_cache_retention_seconds < 60:
            errors.append("raw_cache_retention_seconds 必须是不小于60的有限数")
        if not _finite_number(self.max_receive_rate_per_sender) or self.max_receive_rate_per_sender <= 0:
            errors.append("max_receive_rate_per_sender 必须是有限正数")

        minimum_capacity = 0
        if (
            _finite_number(self.raw_cache_retention_seconds)
            and self.raw_cache_retention_seconds > 0
            and _finite_number(self.max_receive_rate_per_sender)
            and self.max_receive_rate_per_sender > 0
        ):
            minimum_capacity = math.ceil(
                float(self.raw_cache_retention_seconds)
                * float(self.max_receive_rate_per_sender)
            )
        for name, value in (
            ("context_queue_capacity_per_sender", self.context_queue_capacity_per_sender),
            ("raw_cache_capacity_per_sender", self.raw_cache_capacity_per_sender),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum_capacity:
                errors.append(f"{name} 必须是大于等于{minimum_capacity}的整数")

        if self.context_before_packet_count != 20 or isinstance(
            self.context_before_packet_count, bool
        ):
            errors.append("context_before_packet_count 必须固定为20")
        if (
            not _finite_number(self.cache_cleanup_interval_seconds)
            or self.cache_cleanup_interval_seconds <= 0
        ):
            errors.append("cache_cleanup_interval_seconds 必须是有限正数")

        unknown = set(self.hard_value_ranges) - _RANGE_CHANNELS
        if unknown:
            errors.append("hard_value_ranges 包含未知通道: " + ", ".join(sorted(unknown)))
        for channel, value_range in self.hard_value_ranges.items():
            if not isinstance(value_range, ValueRange) or not value_range.valid():
                errors.append(f"{channel} 的硬性允许范围不合法")
        return errors


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
