"""Lifecycle wrapper for the embedded read-only Uvicorn server."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock, Thread
import time
from typing import Any, Protocol

from fastapi import FastAPI
import uvicorn

from plugins.base import BasePlugin, PluginContext

from .app import create_app
from .routes import ApiReadService, HealthProvider


class Server(Protocol):
    should_exit: bool
    started: bool

    def run(self) -> None: ...


ServerFactory = Callable[[FastAPI, str, int], Server]


class ApiPlugin(BasePlugin):
    name = "api"
    dependencies = ()
    required = False

    def __init__(
        self,
        health_provider: HealthProvider,
        *,
        server_factory: ServerFactory | None = None,
        shutdown_timeout_seconds: float = 5.0,
        startup_timeout_seconds: float = 5.0,
    ) -> None:
        if shutdown_timeout_seconds < 0:
            raise ValueError("shutdown_timeout_seconds cannot be negative")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be greater than zero")
        self._health_provider = health_provider
        self._server_factory = server_factory or self._default_server
        self._context: PluginContext | None = None
        self._app: FastAPI | None = None
        self._server: Server | None = None
        self._thread: Thread | None = None
        self._lock = Lock()
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._started = False
        self._stopped = False
        self._stop_requested = False

    @property
    def app(self) -> FastAPI:
        if self._app is None:
            raise RuntimeError("API plugin is not initialized")
        return self._app

    def initialize(self, context: PluginContext) -> None:
        with self._lock:
            if self._context is not None:
                raise RuntimeError("API plugin is already initialized")
            self._context = context
            service = ApiReadService(
                context.runtime_store,
                context.config,
                self._health_provider,
            )
            self._app = create_app(service)
            api_config = context.config.plugins.api
            self._server = self._server_factory(
                self._app,
                api_config.host,
                api_config.port,
            )
            self._stopped = False
            self._stop_requested = False

    def start(self) -> None:
        with self._lock:
            if self._server is None:
                raise RuntimeError("API plugin must be initialized before start")
            if self._started:
                raise RuntimeError("API plugin is already started")
            if self._stopped:
                raise RuntimeError("stopped API plugin cannot be restarted")
            if self._stop_requested:
                raise RuntimeError("API plugin is stopping")
            self._thread = Thread(
                target=self._server.run,
                name="network-api",
                daemon=True,
            )
            self._started = True
            self._thread.start()
            thread = self._thread
            server = self._server
        try:
            self._wait_until_ready(server, thread)
        except Exception:
            with self._lock:
                self._started = False
                self._stop_requested = True
                server.should_exit = True
            thread.join(timeout=self._shutdown_timeout_seconds)
            with self._lock:
                if not thread.is_alive():
                    self._thread = None
                    self._stopped = True
            raise

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._started = False
            self._stop_requested = True
            server = self._server
            thread = self._thread
            if server is not None:
                server.should_exit = True
        if thread is not None:
            thread.join(timeout=self._shutdown_timeout_seconds)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None
                self._stopped = True

    def health(self) -> dict[str, object]:
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            server_ready = self._server is not None and self._server.started
            if self._stop_requested and not thread_alive:
                self._thread = None
                self._stopped = True
            if self._stopped:
                status = "stopped"
            elif self._stop_requested:
                status = "stopping"
            elif self._started and thread_alive and server_ready:
                status = "ok"
            elif self._started and thread_alive:
                status = "starting"
            elif self._started:
                status = "unhealthy"
            elif self._context is not None:
                status = "initialized"
            else:
                status = "created"
            return {"status": status, "thread_alive": thread_alive}

    @staticmethod
    def _default_server(app: FastAPI, host: str, port: int) -> Server:
        return uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="info",
                access_log=False,
            )
        )

    def _wait_until_ready(self, server: Server, thread: Thread) -> None:
        deadline = time.monotonic() + self._startup_timeout_seconds
        while True:
            if server.started and thread.is_alive():
                return
            if not thread.is_alive():
                raise RuntimeError("API server exited before becoming ready")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("API server readiness timed out")
            time.sleep(min(0.01, remaining))
