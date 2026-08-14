"""Lifecycle-managed dependency container for the scheduler service."""
# 该模块统一管理调度器依赖及后台派发线程的启动、恢复和停止。

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from common.config import load_config, service_url

from .assignment_scheduler import AssignmentScheduler
from .cloud_registry import CloudNodeRegistry
from .deferred_cloud_repository import DeferredCloudRepository
from .deferred_dispatcher import DeferredCloudDispatcher
from .deferred_device_dispatcher import DeferredDeviceArbitrationDispatcher
from .deferred_device_repository import DeferredDeviceArbitrationRepository
from .device_router import DeviceArbitrationRouter
from .device_service import DeviceArbitrationService
from .node_registry import NodeRegistry
from .packet_router import PacketRouter
from .packet_service import PacketRoutingService
from .routing_config import load_device_arbitration_config, load_packet_routing_config
from .rule_scheduler import decide_schedule_v01
from .task_repository import TaskRepository


class SchedulerRuntime:
    """Own scheduler dependencies and start/stop their background workers."""

    def __init__(
        self,
        *,
        database_path: Path | str | None = None,
        config: Mapping[str, Any] | None = None,
        dispatcher_client: Any | None = None,
        device_dispatcher_client: Any | None = None,
    ) -> None:
        self.config = dict(config or load_config())
        cloud_config = self.config.get("cloud_node", {})
        status_ttl_ns = int(float(cloud_config.get("status_ttl_seconds", 5)) * 1_000_000_000)
        deferred_config = self.config.get("deferred_cloud_review", {})
        self.dispatcher_interval_seconds = float(
            deferred_config.get("dispatcher_interval_seconds", 1.0)
        )

        self.node_registry = NodeRegistry()
        self.cloud_registry = CloudNodeRegistry(status_ttl_ns=status_ttl_ns)
        self.task_repository = TaskRepository(database_path)
        self.deferred_repository = DeferredCloudRepository(
            self.task_repository.database_path
        )
        self.deferred_device_repository = DeferredDeviceArbitrationRepository(
            self.task_repository.database_path
        )
        self.assignment_scheduler = AssignmentScheduler(
            self.node_registry,
            self.task_repository,
        )
        self.packet_router = PacketRouter(
            assignment_lookup=self.task_repository.get,
            cloud_registry=self.cloud_registry,
            config=load_packet_routing_config(),
        )
        self.packet_service = PacketRoutingService(
            self.packet_router,
            self.deferred_repository,
        )
        self.deferred_dispatcher = DeferredCloudDispatcher(
            self.deferred_repository,
            edge_url_lookup=self.node_registry.control_url,
            client=dispatcher_client,
            eligibility_check=self.packet_router.cloud_delivery_eligibility,
        )
        self.device_router = DeviceArbitrationRouter(
            cloud_registry=self.cloud_registry,
            config=load_device_arbitration_config(),
        )
        self.device_service = DeviceArbitrationService(
            self.device_router,
            self.deferred_device_repository,
        )
        self.deferred_device_dispatcher = DeferredDeviceArbitrationDispatcher(
            self.deferred_device_repository,
            cloud_url_lookup=lambda _cloud_node_id: service_url("cloud", self.config),
            client=device_dispatcher_client,
            eligibility_check=self.device_router.cloud_delivery_eligibility,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.node_registry.start_monitor()
        self.deferred_dispatcher.start(self.dispatcher_interval_seconds)
        self.deferred_device_dispatcher.start(self.dispatcher_interval_seconds)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self.deferred_dispatcher.stop()
        self.deferred_device_dispatcher.stop()
        self.node_registry.stop_monitor()
        self._started = False

    def decide(self, request: Mapping[str, Any], *, v01: bool = False) -> dict[str, Any]:
        if v01:
            return decide_schedule_v01(request)
        return self.assignment_scheduler.decide(request).to_dict()

    def update_edge_node_status(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.node_registry.update_status(request)

    def update_cloud_node_status(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.cloud_registry.update_status(request)

    def update_link_snapshot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if {"link_id", "source_id", "target_id"} <= set(request):
            return self.cloud_registry.update_link(request)
        return self.node_registry.update_link(request)

    def save_task_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.assignment_scheduler.save_result(request)

    def route_packet(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.packet_service.route(request)

    def save_cloud_upload_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.packet_service.save_upload_result(request)

    def route_device_arbitration(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.device_service.route(request)

    def save_device_arbitration_result(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.device_service.save_arbitration_result(request)

    def health(self) -> dict[str, Any]:
        scheduler_config = self.config["services"]["scheduler"]
        return {
            "service": "scheduler_service",
            "node_id": "scheduler_1",
            "status": "ok",
            "model_loaded": True,
            "model_backend": "rule",
            "device": "cpu",
            "port": scheduler_config["port"],
            "edge_nodes": self.node_registry.status_counts(),
        }
