"""Application service joining device routing with deferred persistence."""
# 该模块衔接设备级路由决策和延期任务持久化流程。

from __future__ import annotations

from typing import Any, Mapping

try:
    from .deferred_device_repository import (
        DAY_NS,
        DeferredDeviceArbitrationRepository,
    )
    from .device_router import DeviceArbitrationRouter, cloud_device_task_id
except ImportError:
    from deferred_device_repository import DAY_NS, DeferredDeviceArbitrationRepository
    from device_router import DeviceArbitrationRouter, cloud_device_task_id


class DeviceArbitrationService:
    def __init__(
        self,
        router: DeviceArbitrationRouter,
        repository: DeferredDeviceArbitrationRepository,
    ) -> None:
        self.router = router
        self.repository = repository

    def route(self, request: Mapping[str, Any]) -> dict[str, Any]:
        decision = self.router.decide(request)
        if decision["needs_cloud_arbitration"]:
            task = {
                "decision_id": decision["decision_id"],
                "cloud_task_id": cloud_device_task_id(decision["decision_id"]),
                "device_id": decision["device_id"],
                "task_id": decision["task_id"],
                "summary_module_id": decision["target"]["summary_module_id"],
                "route": decision["route"],
                "reason_codes": decision["reason_codes"],
                "defer_reason": decision["defer_reason"],
                "cloud_status_message_id": decision["input_snapshot"]["cloud_status_message_id"],
                "network_snapshot_id": decision["input_snapshot"]["network_snapshot_id"],
                "bearing_results_ref": decision["source"]["bearing_results_ref"],
                "provisional_result_ref": decision["source"]["provisional_result_ref"],
                "cloud_node_id": decision["target"]["cloud_node_id"],
                "endpoint": decision["target"]["endpoint"],
                "created_at_ns": decision["created_at_ns"],
                "expires_at_ns": decision["created_at_ns"] + DAY_NS,
            }
            self.repository.create(task)
        return decision

    def save_arbitration_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.repository.save_arbitration_result(request)
