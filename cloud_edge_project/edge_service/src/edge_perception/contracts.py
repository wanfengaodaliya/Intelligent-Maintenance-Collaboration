# -*- coding: utf-8 -*-
"""降采样和感知模块的最小调用契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


DOWNSAMPLING_FAILED = "DOWNSAMPLING_FAILED"
PERCEPTION_FAILED = "PERCEPTION_FAILED"


@dataclass(frozen=True)
class ModuleStatus:
    success: bool
    error_code: Optional[str]


@dataclass(frozen=True)
class ModuleResult:
    status: ModuleStatus
    payload: Optional[dict[str, Any]]

    @classmethod
    def succeeded(cls, payload: dict[str, Any]) -> "ModuleResult":
        return cls(ModuleStatus(True, None), payload)

    @classmethod
    def failed(cls, error_code: str) -> "ModuleResult":
        return cls(ModuleStatus(False, error_code), None)


@dataclass(frozen=True)
class PerceptionInvocationContext:
    edge_node_id: str
    perception_received_at_ns: int
