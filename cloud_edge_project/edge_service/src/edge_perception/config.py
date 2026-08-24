# -*- coding: utf-8 -*-
"""感知配置薄转发层。

配置定义已收敛到唯一权威位置 `scenarios.bearing.edge.settings`。
本模块仅重导出配置类型与辅助函数，不再持有独立副本。
"""
from __future__ import annotations

from compatibility.bearing_v12.edge_perception_exports import (
    ConstantDetectionConfig,
    PerceptionConfig,
    file_sha256,
)

__all__ = ["ConstantDetectionConfig", "PerceptionConfig", "file_sha256"]
