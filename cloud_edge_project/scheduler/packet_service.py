"""Application service joining packet routing with deferred-task persistence."""
# 该模块衔接包级路由决策和延期任务持久化流程。

from __future__ import annotations

from typing import Any, Mapping

from compatibility.bearing_v12.scheduler_mapper import (
    packet_decision_to_domain,
    packet_decision_to_legacy,
    packet_result_to_domain,
    packet_result_to_legacy,
)

try:
    from .deferred_cloud_repository import DAY_NS, DeferredCloudRepository
    from .packet_router import (
        PacketRouter,
        cloud_task_id,
    )
except ImportError:
    from deferred_cloud_repository import DAY_NS, DeferredCloudRepository
    from packet_router import PacketRouter, cloud_task_id


class PacketRoutingService:
    def __init__(self, router: PacketRouter, repository: DeferredCloudRepository) -> None:
        self.router = router
        self.repository = repository

    def route(self, request: Mapping[str, Any]) -> dict[str, Any]:
        domain_request = packet_result_to_domain(request)
        legacy_request = packet_result_to_legacy(domain_request)
        decision = self.router.decide(domain_request)
        if decision["needs_cloud_review"]:
            persisted = self.repository.routing_decision(
                decision["decision_id"],
                legacy_request,
            )
            if persisted is not None:
                return packet_decision_to_legacy(
                    packet_decision_to_domain(persisted)
                )
            edge_node_id = str(domain_request["edge_node_id"])
            task = {
                "decision_id": decision["decision_id"],
                "cloud_task_id": cloud_task_id(decision["decision_id"]),
                "device_id": decision["device_id"],
                "task_id": decision["task_id"],
                "unit_id": decision["unit_id"],
                "decision_round_id": decision["decision_round_id"],
                "diagnosis_window_id": decision["diagnosis_window_id"],
                "window_start_sequence": decision["window_start_sequence"],
                "window_end_sequence": decision["window_end_sequence"],
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
                    f"{decision['unit_id']}/{decision['packet_id']}"
                ),
                "context_ref": (
                    f"edge-cache://{edge_node_id}/{decision['task_id']}/"
                    f"{decision['unit_id']}/context/{decision['packet_id']}"
                ),
                "cloud_node_id": decision["target"]["cloud_node_id"],
                "endpoint": decision["target"]["endpoint"],
                "created_at_ns": decision["created_at_ns"],
                "expires_at_ns": decision["created_at_ns"] + DAY_NS,
            }
            self.repository.create(
                packet_decision_to_legacy(task),
                packet_request=legacy_request,
                routing_decision=packet_decision_to_legacy(decision),
            )
            persisted = self.repository.routing_decision(
                decision["decision_id"],
                legacy_request,
            )
            if persisted is not None:
                return packet_decision_to_legacy(
                    packet_decision_to_domain(persisted)
                )
        return packet_decision_to_legacy(decision)

    def save_upload_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.repository.save_upload_result(request)
