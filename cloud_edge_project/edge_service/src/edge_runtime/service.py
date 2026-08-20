# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import Any, Optional

from edge_model.pipeline import EdgeModelPipeline
from edge_validation_cache import EdgeValidationCache

from .config import EdgeRuntimeConfig
from .http import EdgeControlApplication, HeartbeatLoop, make_control_server
from .maintenance import EdgeMaintenanceWorker
from .model_update_poller import ModelUpdatePoller
from .mqtt import MqttIngress


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
        heartbeat: HeartbeatLoop | None,
        maintenance: EdgeMaintenanceWorker | None = None,
        model_update_poller: ModelUpdatePoller | None = None,
        coordinator: Any = None,
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
        self.maintenance = maintenance
        self.model_update_poller = model_update_poller
        self.coordinator = coordinator
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        # 接入流量前全量补齐历史设备结果的双通道出站记录；失败则启动失败，
        # 不允许在已知交付缺口下继续接单。
        if self.coordinator is not None:
            self.coordinator.reconcile_device_result_deliveries(force=True)
        self.cache.start()
        try:
            self.pipeline.start()
            # H1/H3：完成分发线程与建议线程在数据面(推理)之后、接入(MQTT)之前启动，
            # 保证所有完成事件与设备级结果都经后台线程异步处理。
            if self.coordinator is not None:
                self.coordinator.start_background()
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
            if self.heartbeat is not None:
                self.heartbeat.start()
            # 维护线程最后启动：确保 MQTT 与控制通道就绪后再推进业务维护。
            if self.maintenance is not None:
                self.maintenance.start()
            if self.model_update_poller is not None:
                self.model_update_poller.start()
        except Exception:
            self.stop()
            raise
        self._started = True

    def stop(self) -> None:
        # 维护线程最先停止：不再领取新的维护任务，并等待当前轮次结束。
        if self.model_update_poller is not None:
            self.model_update_poller.stop()
        if self.maintenance is not None:
            self.maintenance.stop()
        if self.heartbeat is not None:
            self.heartbeat.stop()
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
        # H1：dispatcher 在 pipeline 之前停，先排空在途完成事件，再停数据面。
        if self.coordinator is not None:
            self.coordinator.stop_background()
        if self.pipeline.started:
            self.pipeline.stop()
        self.cache.stop()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started
