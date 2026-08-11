"""Application service joining packet routing with deferred-task persistence."""
# 该模块衔接包级路由决策和延期任务持久化流程。

from __future__ import annotations

from typing import Any, Mapping

try:
    from .deferred_cloud_repository import DAY_NS, DeferredCloudRepository
    from .packet_router import (
        CLOUD_REVIEW_NOW,
        PacketRouter,
        cloud_task_id,
    )
except ImportError:
    from deferred_cloud_repository import DAY_NS, DeferredCloudRepository
    from packet_router import CLOUD_REVIEW_NOW, PacketRouter, cloud_task_id


class PacketRoutingService:
    def __init__(self, router: PacketRouter, repository: DeferredCloudRepository) -> None:
        self.router = router
        self.repository = repository

    def route(self, request: Mapping[str, Any]) -> dict[str, Any]:
        decision = self.router.decide(request)
        if decision["needs_cloud_review"]:
            edge_node_id = str(request["edge_node_id"])
            task = {
                "decision_id": decision["decision_id"],
                "cloud_task_id": cloud_task_id(decision["decision_id"]),
                "device_id": decision["device_id"],
                "task_id": decision["task_id"],
                "bearing_id": decision["bearing_id"],
                "packet_id": decision["packet_id"],
                "sequence_number": decision["sequence_number"],
                "edge_node_id": edge_node_id,
                "route": decision["route"],
                "reason_codes": decision["reason_codes"],
                "defer_reason": decision["defer_reason"],
                "cloud_status_message_id": decision["input_snapshot"]["cloud_status_message_id"],
                "network_snapshot_id": decision["input_snapshot"]["network_snapshot_id"],
                "raw_data_ref": (
                    f"edge-cache://{edge_node_id}/{decision['task_id']}/"
                    f"{decision['bearing_id']}/{decision['packet_id']}"
                ),
                "context_ref": (
                    f"edge-cache://{edge_node_id}/{decision['task_id']}/"
                    f"{decision['bearing_id']}/context/{decision['packet_id']}"
                ),
                "cloud_node_id": decision["target"]["cloud_node_id"],
                "endpoint": decision["target"]["endpoint"],
                "created_at_ns": decision["created_at_ns"],
                "expires_at_ns": decision["created_at_ns"] + DAY_NS,
            }
            self.repository.create(task)
        return decision

    def save_upload_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.repository.save_upload_result(request)
