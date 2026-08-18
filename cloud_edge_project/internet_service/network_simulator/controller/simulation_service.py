"""Explicit single-tick orchestration for the V3 network simulation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from controller.config_loader import ApplicationConfig
from controller.event_bus import EventBus
from controller.runtime_store import RuntimeStore
from domain.events import (
    LinkScoreCalculated,
    LinkStateApplied,
    LinkStateGenerated,
    TickStarted,
)
from domain.models import (
    SCORE_COMPONENT_NAMES,
    ApplyResult,
    RuntimeSnapshot,
    ScoreResult,
)
from plugins.logger.plugin import LoggerPlugin
from plugins.markov.plugin import MarkovPlugin
from plugins.reporter.plugin import ReporterPlugin
from plugins.score.plugin import ScorePlugin
from plugins.toxiproxy.plugin import ToxiproxyPlugin


@dataclass(frozen=True, slots=True)
class TickExecutionResult:
    tick: int
    timestamp_ns: int
    snapshot: RuntimeSnapshot
    generated_link_count: int
    applied_link_count: int
    scored_link_count: int
    error_count: int


class NetworkSimulationService:
    def __init__(
        self,
        *,
        config: ApplicationConfig,
        runtime_store: RuntimeStore,
        event_bus: EventBus,
        state_plugin: MarkovPlugin,
        toxiproxy_plugin: ToxiproxyPlugin,
        score_plugin: ScorePlugin,
        logger_plugin: LoggerPlugin,
        reporter_plugin: ReporterPlugin | None = None,
    ) -> None:
        self._config = config
        self._runtime_store = runtime_store
        self._event_bus = event_bus
        self._state_plugin = state_plugin
        self._toxiproxy_plugin = toxiproxy_plugin
        self._score_plugin = score_plugin
        self._logger_plugin = logger_plugin
        self._reporter_plugin = reporter_plugin

    @property
    def next_tick(self) -> int:
        return self._runtime_store.last_tick + 1

    def run_tick(self, tick: int, *, timestamp_ns: int) -> TickExecutionResult:
        if tick != self.next_tick:
            raise ValueError(f"tick must equal the next runtime tick ({self.next_tick})")
        if timestamp_ns < 0:
            raise ValueError("timestamp_ns cannot be negative")

        before_links = self._runtime_store.list_links()
        before_by_id = {link.link_id: link for link in before_links}
        self._event_bus.publish(TickStarted(tick, timestamp_ns))

        generated_ids: list[str] = []
        errors: dict[str, str] = {}
        for link in before_links:
            try:
                parameters = self._state_plugin.generate(link.link_id)
                self._runtime_store.update_generated_state(
                    link.link_id,
                    parameters.state,
                    parameters,
                    timestamp_ns,
                    tick,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: state generation failed"
                errors[link.link_id] = error
                self._runtime_store.update_generation_failure(
                    link.link_id,
                    generation=tick,
                    error=error,
                )
            else:
                generated_ids.append(link.link_id)
                self._event_bus.publish(LinkStateGenerated(tick, link.link_id))

        apply_results = self._toxiproxy_plugin.apply_all(
            generation=tick,
            timestamp_ns=timestamp_ns,
            link_ids=generated_ids,
        )
        apply_by_id = self._index_apply_results(apply_results, generated_ids)
        for link_id, result in apply_by_id.items():
            self._event_bus.publish(LinkStateApplied(tick, link_id, result.success))
            if not result.success:
                errors[link_id] = result.error or "Toxiproxy apply failed"

        score_by_id: dict[str, ScoreResult] = {}
        score_errors: dict[str, str] = {}
        for link_id in generated_ids:
            try:
                score_by_id[link_id] = self._score_plugin.calculate(
                    link_id,
                    generation=tick,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: score calculation failed"
                score_errors[link_id] = error
                errors.setdefault(link_id, error)
                self._runtime_store.update_score(
                    link_id,
                    score=0.0,
                    components={name: 0.0 for name in SCORE_COMPONENT_NAMES},
                    available=False,
                    generation=tick,
                )
            else:
                self._event_bus.publish(
                    LinkScoreCalculated(
                        tick,
                        link_id,
                        score_by_id[link_id].score,
                    )
                )

        self._runtime_store.complete_tick(tick, timestamp_ns)
        snapshot = self._runtime_store.snapshot(generated_at_ns=timestamp_ns)
        self._write_tick_logs(
            tick,
            before_by_id,
            snapshot,
            apply_by_id,
            score_by_id,
            score_errors,
            errors,
        )

        reporter_failed = False
        if self._reporter_plugin is not None:
            try:
                self._reporter_plugin.submit(snapshot, now_ns=timestamp_ns)
            except Exception as exc:
                reporter_failed = True
                self._safe_log(
                    self._logger_plugin.log_reporter_operation,
                    {
                        "report_sequence": None,
                        "link_count": len(snapshot.links),
                        "success": False,
                        "status_code": None,
                        "retry_count": 0,
                        "duration_ms": 0.0,
                        "error": f"{type(exc).__name__}: report submission failed",
                    },
                )

        return TickExecutionResult(
            tick=tick,
            timestamp_ns=timestamp_ns,
            snapshot=snapshot,
            generated_link_count=len(generated_ids),
            applied_link_count=sum(result.success for result in apply_results),
            scored_link_count=len(score_by_id),
            error_count=len(errors) + int(reporter_failed),
        )

    @staticmethod
    def _index_apply_results(
        results: tuple[ApplyResult, ...],
        expected_link_ids: list[str],
    ) -> dict[str, ApplyResult]:
        indexed: dict[str, ApplyResult] = {}
        for result in results:
            if result.link_id in indexed:
                raise RuntimeError(f"duplicate apply result for {result.link_id}")
            indexed[result.link_id] = result
        if set(indexed) != set(expected_link_ids):
            raise RuntimeError("Toxiproxy apply results do not match generated links")
        return indexed

    def _write_tick_logs(
        self,
        tick: int,
        before_by_id: dict[str, Any],
        snapshot: RuntimeSnapshot,
        apply_by_id: dict[str, ApplyResult],
        score_by_id: dict[str, ScoreResult],
        score_errors: dict[str, str],
        errors: dict[str, str],
    ) -> None:
        for link in snapshot.links:
            before = before_by_id[link.link_id]
            apply_result = apply_by_id.get(link.link_id)
            if self._config.experiment.save_ground_truth:
                self._safe_log(
                    self._logger_plugin.log_state_update,
                    {
                        "tick": tick,
                        "link_id": link.link_id,
                        "protocol": link.protocol,
                        "old_state": before.current_state,
                        "new_state": link.current_state,
                        "desired_parameters": (
                            None
                            if link.desired_parameters is None
                            else asdict(link.desired_parameters)
                        ),
                        "generation_success": link.link_id in apply_by_id,
                        "apply_success": (
                            False if apply_result is None else apply_result.success
                        ),
                        "error": errors.get(link.link_id),
                    },
                )

            score = score_by_id.get(link.link_id)
            self._safe_log(
                self._logger_plugin.log_score_calculation,
                {
                    "tick": tick,
                    "link_id": link.link_id,
                    "success": score is not None,
                    "score": None if score is None else score.score,
                    "components": None if score is None else dict(score.components),
                    "weights": None if score is None else dict(score.weights),
                    "available": False if score is None else score.available,
                    "reason": None if score is None else score.reason,
                    "packet_loss_applied": (
                        False if score is None else score.packet_loss_applied
                    ),
                    "error": score_errors.get(link.link_id) or errors.get(link.link_id),
                },
            )

    @staticmethod
    def _safe_log(callback: Any, event: dict[str, object]) -> None:
        try:
            callback(event)
        except Exception:
            return
