# -*- coding: utf-8 -*-
"""任务接入模块的显式节点配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskIngressConfig:
    edge_node_id: str

    def validate(self) -> list[str]:
        if not isinstance(self.edge_node_id, str) or not self.edge_node_id.strip():
            return ["edge_node_id 必须是非空字符串"]
        return []
