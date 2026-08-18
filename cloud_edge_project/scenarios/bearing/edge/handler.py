from __future__ import annotations

from typing import Any, Callable

from core.edge_perception_contracts import (
    ModuleResult,
    PerceptionInvocationContext,
)

from .processor import BearingEdgePerception
from .settings import PerceptionConfig


class BearingEdgePerceptionHandler:
    """轴承场景适配器；内部继续使用已验证的原感知实现。"""

    scenario_type = "bearing"

    def __init__(
        self,
        config: PerceptionConfig,
        *,
        processor_factory: Callable[..., BearingEdgePerception] = BearingEdgePerception,
        **processor_options: Any,
    ) -> None:
        self._processor = processor_factory(config, **processor_options)

    @property
    def near_zero_current_count(self) -> int:
        return self._processor.near_zero_current_count

    def downsample(
        self,
        raw_packet: dict[str, Any],
        context: PerceptionInvocationContext,
    ) -> ModuleResult:
        return self._processor.downsample(raw_packet, context)

    def perceive(
        self,
        packet: dict[str, Any],
        context: PerceptionInvocationContext,
    ) -> ModuleResult:
        return self._processor.perceive(packet, context)
