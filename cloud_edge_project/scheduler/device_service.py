"""Application service joining device routing with deferred persistence."""
# 该模块衔接设备级路由决策和延期任务持久化流程。

from __future__ import annotations

from typing import Any, Mapping

from compatibility.bearing_v12.scheduler_mapper import (
    device_payload_to_legacy,
)

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
                "edge_node_id": decision["callback"]["edge_node_id"],
                "route": decision["route"],
                "reason_codes": decision["reason_codes"],
                "defer_reason": decision["defer_reason"],
                "cloud_status_message_id": decision["input_snapshot"]["cloud_status_message_id"],
                "network_snapshot_id": decision["input_snapshot"]["network_snapshot_id"],
                "unit_results_ref": decision["source"]["unit_results_ref"],
                "provisional_result_ref": decision["source"]["provisional_result_ref"],
                "cloud_node_id": decision["target"]["cloud_node_id"],
                "endpoint": decision["target"]["endpoint"],
                "created_at_ns": decision["created_at_ns"],
                "expires_at_ns": decision["created_at_ns"] + DAY_NS,
            }
            if "decision_round_id" in decision:
                task.update(
                    {
                        "conflict_id": decision["conflict_id"],
                        "decision_round_id": decision["decision_round_id"],
                        "device_result_revision": decision["device_result_revision"],
                        "unit_result_ids": decision["unit_result_ids"],
                        "unit_results": decision["unit_results"],
                        "comparison": decision["comparison"],
                        "local_arbitration_supported": decision[
                            "local_arbitration_supported"
                        ],
                    }
                )
            self.repository.create(device_payload_to_legacy(task))
        return device_payload_to_legacy(decision)

    def save_arbitration_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.repository.save_arbitration_result(request)
