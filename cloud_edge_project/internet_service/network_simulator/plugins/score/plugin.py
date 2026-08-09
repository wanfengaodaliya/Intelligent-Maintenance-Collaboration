"""Score plugin that reads applied snapshots and writes score results."""

from __future__ import annotations

from domain.models import ScoreCalculationOutcome, ScoreResult
from plugins.base import BasePlugin, PluginContext

from .calculator import LinkReliabilityCalculator


class ScorePlugin(BasePlugin):
    name = "score"
    dependencies = ("toxiproxy",)
    required = True

    def __init__(self) -> None:
        self._context: PluginContext | None = None
        self._calculator: LinkReliabilityCalculator | None = None
        self._started = False
        self._stopped = False

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise RuntimeError("score plugin is already initialized")
        self._context = context
        self._calculator = LinkReliabilityCalculator(context.config.score)
        self._stopped = False

    def start(self) -> None:
        if self._context is None or self._calculator is None:
            raise RuntimeError("score plugin must be initialized before start")
        if self._started:
            raise RuntimeError("score plugin is already started")
        self._started = True
        self._stopped = False

    def calculate(self, link_id: str, *, generation: int) -> ScoreResult:
        if not self._started or self._context is None or self._calculator is None:
            raise RuntimeError("score plugin must be started before calculation")
        snapshot = self._context.runtime_store.get_link(link_id)
        applied = snapshot.applied_parameters
        scoring_state = applied.state if applied is not None else snapshot.current_state
        result = self._calculator.calculate(
            scoring_state,
            applied,
            snapshot.last_apply_success,
            snapshot.consecutive_apply_failures,
        )
        self._context.runtime_store.update_score(
            link_id,
            score=result.score,
            components=dict(result.components),
            available=result.available,
            generation=generation,
        )
        return result

    def calculate_all(
        self, *, generation: int
    ) -> tuple[ScoreCalculationOutcome, ...]:
        if not self._started or self._context is None:
            raise RuntimeError("score plugin must be started before calculation")
        outcomes: list[ScoreCalculationOutcome] = []
        for link in self._context.runtime_store.list_links():
            try:
                result = self.calculate(link.link_id, generation=generation)
            except Exception as exc:
                outcomes.append(
                    ScoreCalculationOutcome(
                        link_id=link.link_id,
                        success=False,
                        result=None,
                        error_type=type(exc).__name__,
                    )
                )
            else:
                outcomes.append(
                    ScoreCalculationOutcome(
                        link_id=link.link_id,
                        success=True,
                        result=result,
                    )
                )
        return tuple(outcomes)

    def stop(self) -> None:
        self._started = False
        self._stopped = True

    def health(self) -> dict[str, object]:
        if self._stopped:
            status = "stopped"
        elif self._started:
            status = "ok"
        elif self._context is not None:
            status = "initialized"
        else:
            status = "created"
        try:
            link_count = (
                0
                if self._context is None
                else len(self._context.runtime_store.list_links())
            )
        except Exception as exc:
            return {
                "status": "unhealthy",
                "link_count": 0,
                "error_type": type(exc).__name__,
            }
        return {"status": status, "link_count": link_count}
