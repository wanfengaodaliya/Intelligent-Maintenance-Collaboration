# -*- coding: utf-8 -*-
"""任务接入模块的显式节点配置。"""
from __future__ import annotations

import re
from dataclasses import dataclass


EDGE_NODE_ID_PATTERN = re.compile(r"^edge_\d{2,}$")


@dataclass(frozen=True)
class TaskIngressConfig:
    edge_node_id: str
    enforce_edge_node_id_format: bool = False

    def validate(self) -> list[str]:
        if not isinstance(self.edge_node_id, str) or not self.edge_node_id.strip():
            return ["edge_node_id 必须是非空字符串"]
        if self.edge_node_id != self.edge_node_id.strip():
            return ["edge_node_id 不能包含首尾空白"]
        if self.enforce_edge_node_id_format and not EDGE_NODE_ID_PATTERN.fullmatch(
            self.edge_node_id
        ):
            return ["edge_node_id 必须匹配 edge_<至少两位数字>"]
        return []
