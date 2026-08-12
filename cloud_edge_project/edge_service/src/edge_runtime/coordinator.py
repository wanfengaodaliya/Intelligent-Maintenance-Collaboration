# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Optional, Protocol

from core.bearing_workflow_contracts import FINAL_EDGE, FinalPacketResult
from edge_aggregation.workflow import BearingAggregationWorkflow
from edge_aggregation.window_transfer import WindowReviewStore
from edge_model.contracts import PacketExecutionCompleted
from edge_model.pipeline import EdgeModelPipeline
from edge_perception import EdgePerception, PerceptionInvocationContext
from edge_task_ingress import INGRESS_ACCEPTED, EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache

from .contracts import action_level_for
from .http import SchedulerReporter


_INTEGRATION_ONLY_MODEL_VERSION_PREFIX = "bearing-rf-50ms-integration-only-"


class JsonPublisher(Protocol):
    def publish(self, payload: Mapping[str, Any], *, timeout_seconds: float = 2.0) -> None: ...


class EdgeRuntimeCoordinator:
    """Connect task ingress, packet analysis, aggregation and node status reporting."""

    def __init__(
        self,
        *,
        edge_node_id: str,
        ingress: EdgeTaskIngress,
        cache: EdgeValidationCache,
        perception: EdgePerception,
        pipeline: EdgeModelPipeline,
        scheduler: SchedulerReporter,
        aggregation_workflow: Optional[BearingAggregationWorkflow] = None,
        device_result_publisher: Optional[JsonPublisher] = None,
        window_review_store: Optional[WindowReviewStore] = None,
        clock_ns=time.time_ns,
    ):
        self.edge_node_id = edge_node_id
        self.ingress = ingress
        self.cache = cache
        self.perception = perception
        self.pipeline = pipeline
        self.scheduler = scheduler
        self.aggregation_workflow = aggregation_workflow
        self.device_result_publisher = device_result_publisher
        self.window_review_store = window_review_store
        self._clock_ns = clock_ns
        self._pending_aggregation: dict[
            tuple[str, str, str, str, str], FinalPacketResult
        ] = {}
        self._mutex = threading.Lock()
        self._last_task_activity_ns = 0
        self._model_versions: set[str] = set()
        self.pipeline.on_packet_completed = self.on_packet_completed

    def receive_raw_packet(self, raw_packet: dict[str, Any]) -> bool:
        if self.window_review_store is not None:
            self.window_review_store.preflight_packet(raw_packet)
        result = self.ingress.receive_packet(raw_packet)
        self._last_task_activity_ns = max(self._last_task_activity_ns, result.received_at_ns)
        if result.status != INGRESS_ACCEPTED or result.validated_packet is None:
            return result.status != "REJECTED"
        context = PerceptionInvocationContext(
            edge_node_id=self.edge_node_id,
            perception_received_at_ns=result.received_at_ns,
        )
        downsampled = self.perception.downsample(result.validated_packet, context)
        if not downsampled.status.success or downsampled.payload is None:
            self._report_pre_model_failure(
                result.validated_packet,
                error_code=downsampled.status.error_code or "DOWNSAMPLING_FAILED",
                started_at_ns=result.received_at_ns,
            )
            return True
        perceived = self.perception.perceive(downsampled.payload, context)
        if not perceived.status.success or perceived.payload is None:
            self._report_pre_model_failure(
                result.validated_packet,
                error_code=perceived.status.error_code or "PERCEPTION_FAILED",
                started_at_ns=result.received_at_ns,
            )
            return True
        self.pipeline.ingest(raw_packet["sender_id"], perceived.payload)
        return True

    def on_packet_completed(self, completion: PacketExecutionCompleted) -> None:
        packet = self.ingress.packet_snapshot(
            completion.task_id,
            completion.bearing_id,
            completion.sequence_number,
            device_id=completion.device_id,
            sender_id=completion.sender_id,
        )
        task = self.ingress.task_snapshot(
            completion.task_id,
            device_id=completion.device_id,
            sender_id=completion.sender_id,
        )
        if task is None:
            raise ValueError("completed packet has no active task")
        raw_ref = packet.raw_packet_ref if packet is not None else None
        raw_uri = self.cache.raw_data_uri(raw_ref) if raw_ref is not None else None
        output = completion.edge.as_dict() if completion.edge is not None else None
        self.ingress.record_packet_completion(
            device_id=completion.device_id,
            sender_id=completion.sender_id,
            task_id=completion.task_id,
            bearing_id=completion.bearing_id,
            sequence_number=completion.sequence_number,
            output=output,
            error_code=completion.error_code,
            finished_at_ns=completion.finished_at_ns,
        )
        if completion.edge is not None:
            self._model_versions.add(completion.edge.model_version)
        self._aggregate_completion(completion, task.expected_bearing_ids, raw_uri)

    def _aggregate_completion(
        self,
        completion: PacketExecutionCompleted,
        expected_bearing_ids: tuple[str, ...],
        raw_uri: Optional[str],
    ) -> None:
        workflow = self.aggregation_workflow
        if workflow is None or completion.edge is None or raw_uri is None:
            return
        if completion.edge.model_version.startswith(
            _INTEGRATION_ONLY_MODEL_VERSION_PREFIX
        ):
            return
        workflow.register_task(completion.device_id, completion.task_id, expected_bearing_ids)
        edge = completion.edge
        packet = FinalPacketResult(
            result_id=_result_id(completion, 1),
            device_id=completion.device_id,
            task_id=completion.task_id,
            bearing_id=completion.bearing_id,
            sender_id=completion.sender_id,
            packet_id=completion.packet_id,
            sequence_number=completion.sequence_number,
            action_grade=action_level_for(edge.edge_result, edge.edge_risk_level),
            confidence=edge.confidence,
            data_quality_score=completion.data_quality_score,
            risk_level=edge.edge_risk_level,
            decision_source=FINAL_EDGE,
            raw_data_ref=raw_uri,
        )
        try:
            self._accept_aggregation_packet(packet)
        except Exception:
            with self._mutex:
                self._pending_aggregation[_key_from_completion(completion)] = packet

    def _accept_aggregation_packet(self, packet: FinalPacketResult) -> None:
        workflow = self.aggregation_workflow
        if workflow is None:
            return
        device_result = workflow.accept_packet(packet)
        if (
            device_result is not None
            and device_result.status in {"READY", "FINAL"}
            and self.device_result_publisher is not None
        ):
            self.device_result_publisher.publish(device_result.as_dict())

    def node_status(self) -> dict[str, Any]:
        now = self._clock_ns()
        return {
            "edge_node_id": self.edge_node_id,
            "reported_at_ns": now,
            "health_status": "ONLINE",
            "queue_length": self.pipeline.queue_length,
            "models": [
                {"model_version": version, "model_load_status": "LOADED"}
                for version in sorted(self._model_versions)
            ],
            "network_to_scheduler": {
                "measured_at_ns": now,
                "measurement_status": "UNAVAILABLE",
                "available_uplink_mbps_estimate": None,
                "rtt_ms_avg": None,
                "rtt_ms_p95": None,
                "loss_rate": None,
            },
            "last_task_activity_ns": self._last_task_activity_ns or None,
        }

    def report_node_status(self) -> None:
        self._flush_aggregation()
        self.scheduler.report_status(self.node_status())

    def _flush_aggregation(self) -> None:
        with self._mutex:
            pending = sorted(
                self._pending_aggregation.items(),
                key=lambda item: item[1].sequence_number,
            )
        for key, packet in pending:
            try:
                self._accept_aggregation_packet(packet)
            except Exception:
                continue
            with self._mutex:
                if self._pending_aggregation.get(key) == packet:
                    self._pending_aggregation.pop(key, None)

    def _report_pre_model_failure(
        self, packet: Mapping[str, Any], *, error_code: str, started_at_ns: int
    ) -> None:
        completion = PacketExecutionCompleted(
            request_id="pre-model:%s" % packet["packet_id"],
            device_id=packet["device_id"],
            bearing_id=packet["bearing_id"],
            task_id=packet["task_id"],
            packet_id=packet["packet_id"],
            sender_id=packet["sender_id"],
            sequence_number=packet["sequence_number"],
            status="FAILED",
            error_code=error_code,
            started_at_ns=started_at_ns,
            finished_at_ns=self._clock_ns(),
            edge=None,
        )
        self.on_packet_completed(completion)


def _key_from_completion(value: PacketExecutionCompleted) -> tuple[str, str, str, str, str]:
    return (value.device_id, value.sender_id, value.task_id, value.bearing_id, value.packet_id)


def _result_id(value: PacketExecutionCompleted, version: int) -> str:
    return "packet_result_%s_%s_%s_v%d" % (
        value.task_id,
        value.bearing_id,
        value.packet_id,
        version,
    )
