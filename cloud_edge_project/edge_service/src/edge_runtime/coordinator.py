# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping, Optional, Protocol

from core.bearing_actions import action_for_grade
from core.bearing_workflow_contracts import FINAL_EDGE, FinalPacketResult
from core.diagnosis_contracts import EdgeBearingResult
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from edge_aggregation.workflow import BearingAggregationWorkflow
from edge_aggregation.window_transfer import WindowReviewStore
from edge_model.contracts import PacketExecutionCompleted
from edge_model.pipeline import EdgeModelPipeline
from edge_perception import EdgePerception, PerceptionInvocationContext
from edge_task_ingress import INGRESS_ACCEPTED, EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache
from packet_routing_bridge import PacketRoutingBridge
from raw_sample_capture import RawSampleCaptureService
from raw_sample_capture.uploader import RawAnalysisSampleUploader
from result_uploader import ResultUploader

from .contracts import action_level_for
from .http import SchedulerReporter
from .v12_flow import V12DecisionFlow


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
        packet_router: Optional[PacketRoutingBridge] = None,
        v12_flow: Optional[V12DecisionFlow] = None,
        raw_sample_capture: Optional[RawSampleCaptureService] = None,
        raw_sample_uploader: Optional[RawAnalysisSampleUploader] = None,
        result_uploader: Optional[ResultUploader] = None,
        legacy_realtime_aggregation: bool = False,
        cloud_now_timeout_ns: int = 3_000_000_000,
        round_timeout_ns: int = 3_500_000_000,
        on_packet_route_error: Optional[Callable[[dict[str, Any]], None]] = None,
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
        self.packet_router = packet_router
        self.v12_flow = v12_flow
        self.raw_sample_capture = raw_sample_capture
        self.raw_sample_uploader = raw_sample_uploader
        self.result_uploader = result_uploader
        self.legacy_realtime_aggregation = legacy_realtime_aggregation
        self.cloud_now_timeout_ns = cloud_now_timeout_ns
        self.round_timeout_ns = round_timeout_ns
        self.on_packet_route_error = on_packet_route_error or (lambda _: None)
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
        if self.raw_sample_capture is not None:
            try:
                self.raw_sample_capture.record_packet(result.validated_packet)
            except Exception as error:
                self._report_raw_sample_error(result.validated_packet, error)
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
        route_decision = self._route_packet(completion, raw_ref)
        if self.v12_flow is not None and completion.edge is not None and route_decision is not None:
            try:
                raw_packet = self.cache.read(raw_ref) if raw_ref is not None else None
                if raw_packet is None:
                    raise ValueError("completed packet raw data is unavailable")
                edge_result = _edge_bearing_result(completion, raw_packet)
                _, device = self.v12_flow.apply_edge_result(
                    edge_result,
                    route_decision,
                    expected_bearing_ids=task.expected_bearing_ids,
                    accepted_at_ns=self._clock_ns(),
                )
                self._capture_bearing_result(edge_result, route_decision, device)
            except Exception as error:
                self._report_v12_error(completion, error)
            if not self.legacy_realtime_aggregation:
                return
        self._aggregate_completion(completion, task.expected_bearing_ids, raw_uri)

    def _route_packet(
        self,
        completion: PacketExecutionCompleted,
        raw_ref: Any,
    ) -> dict[str, Any] | None:
        if self.packet_router is None:
            return None
        try:
            raw_packet = self.cache.read(raw_ref) if raw_ref is not None else None
            if raw_packet is None:
                raise ValueError("completed packet raw data is unavailable")
            return self.packet_router.route(raw_packet, completion)
        except Exception as error:
            record = {
                "stage": "packet_route",
                "device_id": completion.device_id,
                "task_id": completion.task_id,
                "bearing_id": completion.bearing_id,
                "sender_id": completion.sender_id,
                "packet_id": completion.packet_id,
                "sequence_number": completion.sequence_number,
                "error_code": getattr(error, "code", type(error).__name__),
                "message": str(error),
                "action": "raw_packet_retained_for_replay",
            }
            try:
                self.on_packet_route_error(record)
            except Exception:
                pass
            return None

    def _report_v12_error(self, completion: PacketExecutionCompleted, error: Exception) -> None:
        record = {
            "stage": "v12_decision_flow",
            "device_id": completion.device_id,
            "task_id": completion.task_id,
            "bearing_id": completion.bearing_id,
            "sender_id": completion.sender_id,
            "packet_id": completion.packet_id,
            "sequence_number": completion.sequence_number,
            "error_code": getattr(error, "code", type(error).__name__),
            "message": str(error),
            "action": "raw_packet_retained_for_replay",
        }
        try:
            self.on_packet_route_error(record)
        except Exception:
            pass

    def _capture_bearing_result(
        self,
        edge_result: EdgeBearingResult,
        route_decision: Mapping[str, Any],
        device_result: Any,
    ) -> None:
        if self.raw_sample_capture is None:
            return
        try:
            self.raw_sample_capture.capture(
                {
                    "device_id": edge_result.device_id,
                    "task_id": edge_result.task_id,
                    "bearing_id": edge_result.bearing_id,
                    "sender_id": edge_result.sender_id,
                    "decision_round_id": edge_result.decision_round_id,
                    "confidence": edge_result.confidence,
                    "route": route_decision.get("route"),
                    "bearing_state": edge_result.bearing_state,
                    "risk_level": edge_result.risk_level,
                    "device_conflict": bool(device_result is not None and device_result.has_conflict),
                    "edge_model_version": edge_result.model_version,
                    "created_at_ns": edge_result.window_end_ns,
                }
            )
        except Exception as error:
            self._report_raw_sample_error(edge_result.__dict__, error)

    def _report_raw_sample_error(self, source: Mapping[str, Any], error: Exception) -> None:
        record = {
            "stage": "raw_sample_capture",
            "device_id": source.get("device_id"),
            "task_id": source.get("task_id"),
            "bearing_id": source.get("bearing_id"),
            "sender_id": source.get("sender_id"),
            "error_code": type(error).__name__,
            "message": str(error),
            "action": "real_time_diagnosis_continues",
        }
        try:
            self.on_packet_route_error(record)
        except Exception:
            pass

    def _aggregate_completion(
        self,
        completion: PacketExecutionCompleted,
        expected_bearing_ids: tuple[str, ...],
        raw_uri: Optional[str],
    ) -> None:
        workflow = self.aggregation_workflow
        if workflow is None or completion.edge is None or raw_uri is None:
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
        if self.raw_sample_uploader is not None:
            self.raw_sample_uploader.run_once(self._clock_ns())
        if self.result_uploader is not None:
            self.result_uploader.run_once()
        if self.v12_flow is not None:
            now = self._clock_ns()
            self.v12_flow.promote_cloud_now_timeouts(
                now_ns=now, cloud_now_timeout_ns=self.cloud_now_timeout_ns
            )
            self.v12_flow.finalize_timeouts(
                now_ns=now, round_timeout_ns=self.round_timeout_ns
            )
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


def _edge_bearing_result(
    completion: PacketExecutionCompleted, raw_packet: Mapping[str, Any]
) -> EdgeBearingResult:
    if completion.edge is None:
        raise ValueError("edge bearing result requires a successful edge model output")
    end_ns = raw_packet.get("end_generate_timestamp_ns")
    if isinstance(end_ns, bool) or not isinstance(end_ns, int) or end_ns <= 0:
        raise ValueError("raw packet requires end_generate_timestamp_ns")
    start_ns = max(0, end_ns - 50_000_000)
    window_id = build_diagnosis_window_id(
        device_id=completion.device_id,
        task_id=completion.task_id,
        bearing_id=completion.bearing_id,
        sender_id=completion.sender_id,
        window_start_sequence=completion.sequence_number,
        window_end_sequence=completion.sequence_number,
    )
    action_grade = action_level_for(
        completion.edge.edge_result, completion.edge.edge_risk_level
    )
    return EdgeBearingResult(
        result_id=f"edge_{window_id}_{completion.edge.model_version}",
        device_id=completion.device_id,
        task_id=completion.task_id,
        bearing_id=completion.bearing_id,
        sender_id=completion.sender_id,
        decision_round_id=build_decision_round_id(
            device_id=completion.device_id,
            task_id=completion.task_id,
            window_start_sequence=completion.sequence_number,
            window_end_sequence=completion.sequence_number,
        ),
        diagnosis_window_id=window_id,
        window_start_sequence=completion.sequence_number,
        window_end_sequence=completion.sequence_number,
        window_start_ns=start_ns,
        window_end_ns=end_ns,
        contributing_packet_ids=(completion.packet_id,),
        bearing_state="abnormal" if completion.edge.edge_result == "fault" else completion.edge.edge_result,
        confidence=completion.edge.confidence,
        data_quality_score=completion.data_quality_score,
        risk_level=completion.edge.edge_risk_level,
        action_grade=action_grade,
        recommended_action=action_for_grade(action_grade),
        model_version=completion.edge.model_version,
        created_at_ns=completion.finished_at_ns,
    )
