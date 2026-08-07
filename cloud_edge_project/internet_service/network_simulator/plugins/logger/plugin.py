"""Logger plugin that owns V3 controller and structured event logs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path
import sys
from threading import Lock
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.events import ReportCompleted, ReportDropped, TickStarted
from plugins.base import BasePlugin, PluginContext

from .jsonl_writer import JsonlWriter, sanitize_for_log


class _TimezoneNanosecondFormatter(logging.Formatter):
    def __init__(
        self,
        timezone: ZoneInfo,
        now_ns: Callable[[], int],
    ) -> None:
        super().__init__(
            "%(timestamp)s timestamp_ns=%(timestamp_ns)d "
            "%(levelname)s %(name)s %(message)s"
        )
        self._timezone = timezone
        self._now_ns = now_ns

    def format(self, record: logging.LogRecord) -> str:
        timestamp_ns = getattr(record, "timestamp_ns", None)
        if timestamp_ns is None:
            timestamp_ns = self._now_ns()
        record.timestamp_ns = timestamp_ns
        record.timestamp = datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000,
            tz=self._timezone,
        ).isoformat()
        return super().format(record)


class _BoundedRotatingFileHandler(RotatingFileHandler):
    """Keep rotation bounded when zero backups means discard old content."""

    def doRollover(self) -> None:
        if self.backupCount != 0:
            super().doRollover()
            return
        if self.stream is not None:
            self.stream.seek(0)
            self.stream.truncate()
            self.stream.flush()


class LoggerPlugin(BasePlugin):
    name = "logger"
    dependencies = ()
    required = True

    def __init__(
        self,
        log_dir: str | Path = "logs",
        *,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._log_dir = Path(log_dir)
        self._now_ns = now_ns
        self._context: PluginContext | None = None
        self._timezone = ZoneInfo("UTC")
        self._writers: dict[str, JsonlWriter] = {}
        self._logger: logging.Logger | None = None
        self._started = False
        self._stopped = False
        self._write_failures = 0
        self._failure_lock = Lock()

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise RuntimeError("Logger plugin is already initialized")
        self._context = context
        self._timezone = ZoneInfo(context.config.experiment.timezone)
        self._logger = self._build_controller_logger(
            context.config.logging.max_bytes,
            context.config.logging.backup_count,
        )
        for name in (
            "state_updates",
            "toxic_operations",
            "score_calculations",
            "reporter_operations",
        ):
            self._writers[name] = JsonlWriter(
                self._log_dir / f"{name}.jsonl",
                max_bytes=context.config.logging.max_bytes,
                backup_count=context.config.logging.backup_count,
                error_handler=self._handle_writer_error,
            )
        context.event_bus.subscribe(TickStarted, self._on_tick_started)
        context.event_bus.subscribe(ReportCompleted, self._on_report_completed)
        context.event_bus.subscribe(ReportDropped, self._on_report_dropped)
        self._stopped = False

    def start(self) -> None:
        if self._context is None or self._logger is None:
            raise RuntimeError("Logger plugin must be initialized before start")
        if self._started:
            raise RuntimeError("Logger plugin is already started")
        if self._stopped:
            raise RuntimeError("stopped Logger plugin cannot be restarted")
        self._started = True
        self._logger.info("Logger plugin started")

    def stop(self) -> None:
        if self._stopped:
            return
        if self._context is not None:
            self._context.event_bus.unsubscribe(TickStarted, self._on_tick_started)
            self._context.event_bus.unsubscribe(
                ReportCompleted, self._on_report_completed
            )
            self._context.event_bus.unsubscribe(ReportDropped, self._on_report_dropped)
        if self._logger is not None:
            self._logger.info("Logger plugin stopped")
            for handler in tuple(self._logger.handlers):
                handler.close()
                self._logger.removeHandler(handler)
        self._started = False
        self._stopped = True

    def log_state_update(self, event: Mapping[str, Any]) -> bool:
        return self._write("state_updates", "network_state_update", event)

    def log_toxic_operation(self, event: Mapping[str, Any]) -> bool:
        return self._write("toxic_operations", "toxic_operation", event)

    def log_score_calculation(self, event: Mapping[str, Any]) -> bool:
        return self._write("score_calculations", "score_calculation", event)

    def log_reporter_operation(self, event: Mapping[str, Any]) -> bool:
        return self._write("reporter_operations", "reporter_operation", event)

    def info(self, message: str, *args: object) -> None:
        self._controller_log(logging.INFO, message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._controller_log(logging.WARNING, message, *args)

    def error(self, message: str, *args: object) -> None:
        self._controller_log(logging.ERROR, message, *args)

    def health(self) -> dict[str, object]:
        with self._failure_lock:
            write_failures = self._write_failures
        if self._stopped:
            status = "stopped"
        elif write_failures:
            status = "degraded"
        elif self._started:
            status = "ok"
        elif self._context is not None:
            status = "initialized"
        else:
            status = "created"
        return {"status": status, "write_failures": write_failures}

    def _write(
        self,
        writer_name: str,
        event_type: str,
        event: Mapping[str, Any],
    ) -> bool:
        writer = self._writers.get(writer_name)
        if writer is None:
            return False
        timestamp_ns = self._now_ns()
        payload = {
            **dict(event),
            "event_type": event_type,
            "timestamp": datetime.fromtimestamp(
                timestamp_ns / 1_000_000_000,
                tz=self._timezone,
            ).isoformat(),
            "timestamp_ns": timestamp_ns,
        }
        success = writer.write(payload)
        if not success:
            with self._failure_lock:
                self._write_failures += 1
        return success

    def _on_tick_started(self, event: TickStarted) -> None:
        if self._logger is not None:
            self._logger.info(
                "Tick started",
                extra={"tick": event.tick, "timestamp_ns": event.timestamp_ns},
            )

    def _on_report_completed(self, event: ReportCompleted) -> None:
        self.log_reporter_operation(asdict(event))

    def _on_report_dropped(self, event: ReportDropped) -> None:
        self.log_reporter_operation(
            {
                "report_sequence": event.report_sequence,
                "link_count": 0,
                "success": False,
                "status_code": None,
                "retry_count": 0,
                "duration_ms": 0.0,
                "error": event.reason,
            }
        )

    def _build_controller_logger(
        self,
        max_bytes: int,
        backup_count: int,
    ) -> logging.Logger:
        logger = logging.Logger(f"network_simulator.controller.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = _TimezoneNanosecondFormatter(self._timezone, self._now_ns)
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = _BoundedRotatingFileHandler(
                self._log_dir / "controller.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            with self._failure_lock:
                self._write_failures += 1
            logger.warning(
                "Controller file logging unavailable: %s",
                type(exc).__name__,
            )
        return logger

    def _handle_writer_error(self, path: str, error_type: str) -> None:
        if self._logger is not None:
            self._logger.error(
                "JSONL write failed",
                extra={
                    "path": sanitize_for_log(path),
                    "error_type": error_type,
                },
            )

    def _controller_log(
        self,
        level: int,
        message: str,
        *args: object,
    ) -> None:
        logger = self._logger
        if logger is None or not logger.handlers:
            logger = logging.getLogger(__name__)
        logger.log(level, message, *args)
