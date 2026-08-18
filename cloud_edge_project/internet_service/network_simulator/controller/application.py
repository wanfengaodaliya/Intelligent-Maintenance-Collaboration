"""V3 application composition, signal handling, and exit-code policy."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
import signal
from threading import Event, current_thread, main_thread
import time
import traceback
from types import FrameType
from typing import Any

from controller.config_loader import ApplicationConfig
from controller.event_bus import EventBus
from controller.lifecycle import MonotonicSimulationLoop
from controller.plugin_manager import PluginManager
from controller.runtime_factory import RuntimeFactory
from controller.runtime_store import RuntimeStore
from controller.simulation_service import NetworkSimulationService
from plugins.api.plugin import ApiPlugin
from plugins.base import PluginContext
from plugins.health.plugin import HealthPlugin
from plugins.logger.plugin import LoggerPlugin
from plugins.reporter.plugin import ReporterPlugin
from plugins.score.plugin import ScorePlugin
from plugins.toxiproxy.client import ToxiproxyClient
from plugins.toxiproxy.plugin import ToxiproxyPlugin


class NetworkSimulatorApplication:
    def __init__(
        self,
        *,
        context: PluginContext,
        plugin_manager: PluginManager,
        simulation_loop: MonotonicSimulationLoop,
        logger: LoggerPlugin,
        manage_signals: bool = True,
    ) -> None:
        self._context = context
        self._plugin_manager = plugin_manager
        self._simulation_loop = simulation_loop
        self._logger = logger
        self._manage_signals = manage_signals
        self._previous_handlers: dict[int, Any] = {}

    @property
    def shutdown_event(self) -> Event:
        return self._context.shutdown_event

    def request_stop(
        self,
        signum: int | None = None,
        frame: FrameType | None = None,
    ) -> None:
        del frame
        if signum is not None:
            self._logger.info("Received signal %s; stopping", signum)
        self.shutdown_event.set()

    def run(self) -> int:
        exit_code = 0
        try:
            self._install_signal_handlers()
            self._plugin_manager.initialize(self._context)
            self._plugin_manager.start()
            self._logger.info(
                "Network simulator started: %s",
                self._context.config.sanitized_summary(),
            )
            self._simulation_loop.run()
        except Exception as exc:
            exit_code = 1
            self._logger.error(
                "Application failed (%s); traceback=%s",
                type(exc).__name__,
                safe_traceback(exc),
            )
        finally:
            try:
                self._plugin_manager.stop()
            except Exception as exc:
                exit_code = 1
                self._logger.error(
                    "Application shutdown failed (%s); traceback=%s",
                    type(exc).__name__,
                    safe_traceback(exc),
                )
            self._restore_signal_handlers()
        return exit_code

    def _install_signal_handlers(self) -> None:
        if (
            not self._manage_signals
            or current_thread() is not main_thread()
        ):
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self.request_stop)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            signal.signal(signum, handler)
        self._previous_handlers.clear()


def safe_traceback(error: BaseException) -> str:
    """Return actionable traceback frames without exception messages or locals."""

    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "unavailable"
    return " -> ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames
    )


def build_application(
    config: ApplicationConfig,
    log_dir: str | Path,
    *,
    now_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
) -> NetworkSimulatorApplication:
    assembly = RuntimeFactory(config).build(timestamp_ns=now_ns())
    runtime_store = RuntimeStore(assembly.runtimes)
    event_bus = EventBus()
    shutdown_event = Event()
    context = PluginContext(config, runtime_store, event_bus, shutdown_event)

    logger_plugin = LoggerPlugin(log_dir, now_ns=now_ns)
    toxiproxy_config = config.toxiproxy
    toxiproxy_client = ToxiproxyClient(
        toxiproxy_config.api_base_url,
        toxiproxy_config.connect_timeout_seconds,
        toxiproxy_config.read_timeout_seconds,
        toxiproxy_config.retry_count,
        toxiproxy_config.backoff_base_seconds,
        operation_logger=logger_plugin.log_toxic_operation,
    )
    toxiproxy_plugin = ToxiproxyPlugin(
        toxiproxy_client,
        monotonic=monotonic,
        now_ns=now_ns,
    )
    score_plugin = ScorePlugin()
    reporter_plugin = ReporterPlugin(now_ns=now_ns)
    health_plugin = HealthPlugin(
        toxiproxy_health=toxiproxy_plugin.health,
        reporter_health=reporter_plugin.health,
    )
    api_plugin = ApiPlugin(health_plugin.health)
    plugin_manager = PluginManager(
        (
            logger_plugin,
            assembly.plugin,
            toxiproxy_plugin,
            score_plugin,
            reporter_plugin,
            health_plugin,
            api_plugin,
        ),
        logger=logger_plugin,
    )
    reporter_for_service = (
        reporter_plugin
        if config.reporter.enabled and config.plugins.reporter.enabled
        else None
    )
    service = NetworkSimulationService(
        config=config,
        runtime_store=runtime_store,
        event_bus=event_bus,
        state_plugin=assembly.plugin,
        toxiproxy_plugin=toxiproxy_plugin,
        score_plugin=score_plugin,
        logger_plugin=logger_plugin,
        reporter_plugin=reporter_for_service,
    )
    simulation_loop = MonotonicSimulationLoop(
        service,
        update_interval_seconds=config.controller.update_interval_seconds,
        duration_seconds=config.experiment.duration_seconds,
        shutdown_event=shutdown_event,
        logger=logger_plugin,
        monotonic=monotonic,
        now_ns=now_ns,
    )
    return NetworkSimulatorApplication(
        context=context,
        plugin_manager=plugin_manager,
        simulation_loop=simulation_loop,
        logger=logger_plugin,
    )
