# -*- coding: utf-8 -*-
"""降采样与感知模块调用契约（薄转发层）。

契约定义已收敛到唯一权威位置 `core.edge_perception_contracts`。
本模块仅重导出契约类型与错误码，不再持有任何独立副本。
"""
from __future__ import annotations

from core.edge_perception_contracts import (
    DOWNSAMPLING_FAILED,
    PERCEPTION_FAILED,
    ModuleResult,
    ModuleStatus,
    PerceptionInvocationContext,
)

__all__ = [
    "DOWNSAMPLING_FAILED",
    "PERCEPTION_FAILED",
    "ModuleResult",
    "ModuleStatus",
    "PerceptionInvocationContext",
]