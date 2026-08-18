from __future__ import annotations

from collections.abc import Callable

from core.edge_perception_contracts import PerceptionHandler


PerceptionFactory = Callable[[], PerceptionHandler]


class PerceptionRegistry:
    """在服务入口装配场景感知实现。"""

    def __init__(self) -> None:
        self._factories: dict[str, PerceptionFactory] = {}

    def register(self, scenario_type: str, factory: PerceptionFactory) -> None:
        normalized = _normalize(scenario_type)
        if normalized in self._factories:
            raise ValueError(f"perception scenario already registered: {normalized}")
        self._factories[normalized] = factory

    def create(self, scenario_type: str) -> PerceptionHandler:
        normalized = _normalize(scenario_type)
        factory = self._factories.get(normalized)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ValueError(
                f"unsupported edge perception scenario: {normalized}; available={available}"
            )
        return factory()


def _normalize(value: str) -> str:
    normalized = str(value).strip().lower()
    if not normalized:
        raise ValueError("scenario_type must be a non-empty string")
    return normalized
