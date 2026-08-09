"""Dependency-aware lifecycle manager for in-process plugins."""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from domain.exceptions import ConfigurationError, PluginLifecycleError
from plugins.base import BasePlugin, PluginContext


class PluginManager:
    def __init__(
        self,
        plugins: Iterable[BasePlugin],
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._plugins: dict[str, BasePlugin] = {}
        for plugin in plugins:
            if not plugin.name:
                raise ConfigurationError("plugin name cannot be empty")
            if plugin.name in self._plugins:
                raise ConfigurationError(f"duplicate plugin name: {plugin.name}")
            self._plugins[plugin.name] = plugin

        self._ordered: tuple[BasePlugin, ...] = ()
        self._required: set[str] = set()
        self._initialized: list[BasePlugin] = []
        self._started: list[BasePlugin] = []
        self._status: dict[str, dict[str, object]] = {}
        self._context: PluginContext | None = None
        self._stopped = False
        self._start_invoked = False

    @property
    def ordered_names(self) -> tuple[str, ...]:
        return tuple(plugin.name for plugin in self._ordered)

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise PluginLifecycleError("plugins are already initialized")
        enabled = self._select_enabled_plugins(context.config)
        ordered = self._topological_sort(enabled)
        required = {
            plugin.name
            for plugin in ordered
            if self._is_required(plugin, context.config)
        }
        self._context = context
        self._ordered = ordered
        self._required = required

        for plugin in self._ordered:
            missing = [
                dependency
                for dependency in plugin.dependencies
                if dependency not in {item.name for item in self._initialized}
            ]
            if missing:
                error = f"plugin {plugin.name} dependencies failed: {missing}"
                if plugin.name in self._required:
                    self._rollback_initialized()
                    raise PluginLifecycleError(error)
                self._status[plugin.name] = {"status": "failed", "error": error}
                continue
            try:
                plugin.initialize(context)
            except Exception as exc:
                self._record_failure(plugin, "initialize", exc)
                self._safe_stop(plugin)
                if plugin.name in self._required:
                    self._rollback_initialized()
                    raise PluginLifecycleError(
                        f"required plugin {plugin.name} failed to initialize"
                    ) from None
                continue
            self._initialized.append(plugin)
            self._status[plugin.name] = {"status": "initialized"}

    def start(self) -> None:
        if self._context is None:
            raise PluginLifecycleError("plugins must be initialized before start")
        if self._start_invoked:
            raise PluginLifecycleError("plugins are already started")
        self._start_invoked = True
        self._stopped = False

        for plugin in tuple(self._initialized):
            missing = [
                dependency
                for dependency in plugin.dependencies
                if dependency not in {item.name for item in self._started}
            ]
            if missing:
                error = f"plugin {plugin.name} dependencies did not start: {missing}"
                if plugin.name in self._required:
                    self.stop()
                    raise PluginLifecycleError(error)
                self._status[plugin.name] = {"status": "failed", "error": error}
                continue
            try:
                plugin.start()
            except Exception as exc:
                self._record_failure(plugin, "start", exc)
                self._safe_stop(plugin)
                self._initialized.remove(plugin)
                if plugin.name in self._required:
                    self.stop()
                    raise PluginLifecycleError(
                        f"required plugin {plugin.name} failed to start"
                    ) from None
                continue
            self._started.append(plugin)
            self._status[plugin.name] = {"status": "started"}

    def stop(self) -> None:
        if self._stopped:
            return
        for plugin in reversed(self._initialized):
            self._safe_stop(plugin)
            if self._status.get(plugin.name, {}).get("status") != "failed":
                self._status[plugin.name] = {"status": "stopped"}
        self._started.clear()
        self._stopped = True

    def health(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name, plugin in self._plugins.items():
            status = self._status.get(name)
            if status and status.get("status") in {"disabled", "failed"}:
                result[name] = dict(status)
                continue
            try:
                plugin_health = plugin.health()
                result[name] = dict(plugin_health)
            except Exception as exc:
                result[name] = {
                    "status": "unhealthy",
                    "error": self._error_text(exc),
                }
        return result

    def _select_enabled_plugins(self, config: Any) -> dict[str, BasePlugin]:
        enabled: dict[str, BasePlugin] = {}
        plugin_settings = getattr(config, "plugins", None)
        for name, plugin in self._plugins.items():
            setting = getattr(plugin_settings, name, None)
            if setting is not None and setting.required and not setting.enabled:
                raise ConfigurationError(
                    f"required plugin {name} cannot be disabled"
                )
            if setting is not None and not setting.enabled:
                self._status[name] = {"status": "disabled"}
                continue
            enabled[name] = plugin
        return enabled

    def _is_required(self, plugin: BasePlugin, config: Any) -> bool:
        plugin_settings = getattr(config, "plugins", None)
        setting = getattr(plugin_settings, plugin.name, None)
        return plugin.required if setting is None else bool(setting.required)

    @staticmethod
    def _topological_sort(
        plugins: dict[str, BasePlugin],
    ) -> tuple[BasePlugin, ...]:
        ordered: list[BasePlugin] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ConfigurationError(f"plugin dependency cycle includes {name}")
            plugin = plugins[name]
            visiting.add(name)
            for dependency in plugin.dependencies:
                if dependency not in plugins:
                    raise ConfigurationError(
                        f"plugin {name} has missing dependency {dependency}"
                    )
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(plugin)

        for plugin_name in plugins:
            visit(plugin_name)
        return tuple(ordered)

    def _rollback_initialized(self) -> None:
        for plugin in reversed(self._initialized):
            self._safe_stop(plugin)
        self._initialized.clear()

    def _safe_stop(self, plugin: BasePlugin) -> None:
        try:
            plugin.stop()
        except Exception as exc:
            self._record_failure(plugin, "stop", exc)

    def _record_failure(
        self,
        plugin: BasePlugin,
        phase: str,
        error: Exception,
    ) -> None:
        error_text = self._error_text(error)
        self._status[plugin.name] = {
            "status": "failed",
            "phase": phase,
            "error": error_text,
        }
        self._logger.error(
            "plugin %s %s failed (%s)",
            plugin.name,
            phase,
            type(error).__name__,
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        return f"{type(error).__name__}: plugin operation failed"
