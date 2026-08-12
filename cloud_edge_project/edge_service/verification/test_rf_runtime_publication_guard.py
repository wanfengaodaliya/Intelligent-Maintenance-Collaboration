from __future__ import annotations

from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_runtime.coordinator import EdgeRuntimeCoordinator


class _Pipeline:
    on_packet_completed = None


class _Workflow:
    def __init__(self) -> None:
        self.registered = []

    def register_task(self, *args) -> None:
        self.registered.append(args)


def test_integration_only_model_cannot_enter_final_aggregation() -> None:
    workflow = _Workflow()
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id="edge_01",
        ingress=None,
        cache=None,
        perception=None,
        pipeline=_Pipeline(),
        scheduler=None,
        aggregation_workflow=workflow,
    )
    completion = PacketExecutionCompleted(
        request_id="request-1",
        device_id="device-1",
        bearing_id="bearing-1",
        task_id="task-1",
        packet_id="packet-1",
        sender_id="sender-1",
        sequence_number=1,
        status="COMPLETED",
        error_code=None,
        started_at_ns=1,
        finished_at_ns=2,
        edge=EdgeResult(
            edge_result="fault",
            confidence=0.9,
            edge_risk_level="high",
            model_version="bearing-rf-50ms-integration-only-v1",
        ),
    )

    coordinator._aggregate_completion(completion, ("bearing-1",), "edge-cache://raw")

    assert workflow.registered == []
