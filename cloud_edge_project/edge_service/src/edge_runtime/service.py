# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import Optional

from edge_model.pipeline import EdgeModelPipeline
from edge_validation_cache import EdgeValidationCache

from .config import EdgeRuntimeConfig
from .http import EdgeControlApplication, HeartbeatLoop, make_control_server
from .mqtt import MqttIngress
from edge_aggregation.window_transfer import WindowReviewDispatcher


class EdgeRuntimeService:
    """统一管理边缘运行时的启动顺序和失败回滚。"""

    def __init__(
        self,
        *,
        config: EdgeRuntimeConfig,
        cache: EdgeValidationCache,
        pipeline: EdgeModelPipeline,
        mqtt_ingress: MqttIngress,
        control_application: EdgeControlApplication,
        heartbeat: HeartbeatLoop,
        window_dispatcher: WindowReviewDispatcher,
    ):
        errors = config.validate()
        if errors:
            raise ValueError("边缘运行时配置无效: " + "; ".join(errors))
        self.config = config
        self.cache = cache
        self.pipeline = pipeline
        self.mqtt_ingress = mqtt_ingress
        self.control_application = control_application
        self.heartbeat = heartbeat
        self.window_dispatcher = window_dispatcher
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.cache.start()
        try:
            self.pipeline.start()
            self._server = make_control_server(
                self.config.control.host,
                self.config.control.port,
                self.control_application,
            )
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="edge-control-http",
                daemon=True,
            )
            self._server_thread.start()
            self.mqtt_ingress.start()
            self.window_dispatcher.start()
            self.heartbeat.start()
        except Exception:
            self.stop()
            raise
        self._started = True

    def stop(self) -> None:
        self.heartbeat.stop()
        self.window_dispatcher.stop()
        try:
            self.mqtt_ingress.stop()
        except Exception:
            pass
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._server_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._server = None
        self._server_thread = None
        if self.pipeline.started:
            self.pipeline.stop()
        self.cache.stop()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started
