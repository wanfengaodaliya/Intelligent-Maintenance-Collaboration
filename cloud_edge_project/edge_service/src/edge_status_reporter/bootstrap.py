# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping

import requests

from .collectors import AcceleratorDetector, build_resource_collector
from .config import EdgeStatusReporterConfig
from .middleware import EdgeActivityMiddleware
from .network import SimulationNetworkCollector
from .reporter import EdgeStatusReporter
from .state import EdgeApplicationState
from .transport import HttpStatusTarget


LOGGER = logging.getLogger(__name__)


class EdgeStatusIntegration:
    def __init__(
        self,
        *,
        state: EdgeApplicationState | None,
        reporter: EdgeStatusReporter | None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.state = state
        self.reporter = reporter
        self.logger = logger or LOGGER
        self._installed = False

    @property
    def enabled(self) -> bool:
        return self.reporter is not None and self.state is not None

    def install(self, app: Any) -> None:
        if not self.enabled or self._installed:
            return
        app.add_middleware(EdgeActivityMiddleware, state=self.state)
        self._installed = True

    @asynccontextmanager
    async def lifespan(self, _: Any):
        reporter = self.reporter
        if reporter is not None:
            try:
                reporter.start()
            except Exception:
                self.logger.exception("Edge Status Reporter 启动失败，Edge 继续运行")
        try:
            yield
        finally:
            if reporter is not None:
                try:
                    reporter.stop()
                except Exception:
                    self.logger.exception("Edge Status Reporter 停止失败")


def build_edge_status_integration(
    *,
    edge_node_id: str,
    default_model_version: str,
    environ: Mapping[str, str] | None = None,
    psutil_module: Any | None = None,
    http_post: Callable[..., Any] = requests.post,
    clock_ns: Callable[[], int] = time.time_ns,
    logger: logging.Logger | None = None,
) -> EdgeStatusIntegration:
    selected_logger = logger or LOGGER
    try:
        config = EdgeStatusReporterConfig.from_env(
            default_model_version=default_model_version,
            environ=environ,
        )
    except Exception:
        selected_logger.exception("Edge Status Reporter 配置无效，Reporter 已禁用")
        return EdgeStatusIntegration(state=None, reporter=None, logger=selected_logger)
    if not config.enabled:
        selected_logger.info("Edge Status Reporter 已通过配置关闭")
        return EdgeStatusIntegration(state=None, reporter=None, logger=selected_logger)
    try:
        resource_collector = build_resource_collector(
            config.resource,
            psutil_module=psutil_module,
        )
    except Exception:
        selected_logger.exception("Edge Status Reporter 资源采集器初始化失败，Reporter 已禁用")
        return EdgeStatusIntegration(state=None, reporter=None, logger=selected_logger)
    state = EdgeApplicationState(
        edge_node_id=edge_node_id,
        model_version=config.model_version,
        clock_ns=clock_ns,
    )
    targets = tuple(
        HttpStatusTarget(target, http_post=http_post, logger=selected_logger)
        for target in (config.scheduler, config.cloud)
        if target.enabled
    )
    reporter = EdgeStatusReporter(
        status_source=state,
        resource_collector=resource_collector,
        accelerator_detector=AcceleratorDetector(
            config.accelerator,
            logger=selected_logger,
        ),
        network_collector=SimulationNetworkCollector(
            config.network.url,
            timeout_seconds=config.network.timeout_seconds,
            stale_after_seconds=config.network.stale_after_seconds,
            clock_ns=clock_ns,
        ),
        targets=targets,
        interval_seconds=config.interval_seconds,
        clock_ns=clock_ns,
        logger=selected_logger,
    )
    selected_logger.info(
        "Edge Status Reporter 配置完成 mode=%s scheduler=%s cloud=%s",
        config.resource.mode,
        config.scheduler.url if config.scheduler.enabled else "disabled",
        config.cloud.url if config.cloud.enabled else "disabled",
    )
    return EdgeStatusIntegration(state=state, reporter=reporter, logger=selected_logger)
