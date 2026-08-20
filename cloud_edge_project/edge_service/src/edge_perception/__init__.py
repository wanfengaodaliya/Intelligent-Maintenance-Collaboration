# -*- coding: utf-8 -*-
"""单轴承降采样与感知特征提取（薄转发层）。

感知实现已收敛到唯一权威位置 `scenarios.bearing.edge`，契约收敛到
`core.edge_perception_contracts`。本包仅作为兼容入口重导出权威符号，
不再持有任何独立处理逻辑或资产副本。
"""

from .config import ConstantDetectionConfig, PerceptionConfig, file_sha256
from .contracts import (
    DOWNSAMPLING_FAILED,
    PERCEPTION_FAILED,
    ModuleResult,
    ModuleStatus,
    PerceptionInvocationContext,
)
from .processor import BearingEdgePerception, EdgePerception
from .protocol import PerceptionHandler
from .registry import PerceptionFactory, PerceptionRegistry

__all__ = [
    "BearingEdgePerception",
    "ConstantDetectionConfig",
    "DOWNSAMPLING_FAILED",
    "EdgePerception",
    "ModuleResult",
    "ModuleStatus",
    "PERCEPTION_FAILED",
    "PerceptionConfig",
    "PerceptionFactory",
    "PerceptionHandler",
    "PerceptionInvocationContext",
    "PerceptionRegistry",
    "file_sha256",
]