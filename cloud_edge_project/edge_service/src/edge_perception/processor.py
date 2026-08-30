# -*- coding: utf-8 -*-
"""感知处理器薄转发层。

感知实现已收敛到唯一权威位置 `scenarios.bearing.edge.processor`。
本模块仅重导出 `BearingEdgePerception` 并保留旧兼容名 `EdgePerception`，
不再持有任何独立处理逻辑。
"""
from __future__ import annotations

from compatibility.bearing_v12.edge_perception_exports import BearingEdgePerception

# 旧公共 API 兼容名：historically 通过 `from edge_perception import EdgePerception` 消费。
EdgePerception = BearingEdgePerception

__all__ = ["EdgePerception", "BearingEdgePerception"]
