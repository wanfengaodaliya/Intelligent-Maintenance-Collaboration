# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

from edge_model.contracts import EdgeResult, PacketExecutionCompleted
from edge_model.pipeline import EdgeModelPipeline
from edge_perception import EdgePerception, PerceptionInvocationContext
from edge_task_ingress import INGRESS_ACCEPTED, EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache

from .cloud import CloudPacketUploader
from .contracts import (
    CLOUD_REVIEW_NOW,
    DIRECT_FINAL_TO_SUMMARY,
    EDGE_PROVISIONAL_AND_DEFER_CLOUD,
    RESULT_FINAL,
    RESULT_PROVISIONAL,
    CloudPacketReviewInstruction,
    PacketAnalysisReport,
    PacketRouteDecision,
    SummaryPacketResult,
    action_level_for,
)
from .http import SchedulerReporter


class JsonPublisher(Protocol):
    def publish(self, payload: Mapping[str, Any], *, timeout_seconds: float = 2.0) -> None: ...


@dataclass
class _PacketState:
    report: PacketAnalysisReport
    completion: PacketExecutionCompleted
    raw_packet_ref: Optional[tuple[str, str, int]]
    decision_fingerprints: dict[str, str] = field(default_factory=dict)
    cloud_instruction_fingerprints: dict[str, str] = field(default_factory=dict)
    publishing_results: set[tuple[str, str]] = field(default_factory=set)
    published_results: set[tuple[str, str]] = field(default_factory=set)
    uploading_cloud_tasks: set[str] = field(default_factory=set)
    uploaded_cloud_tasks: set[str] = field(default_factory=set)
    reported_cloud_tasks: set[str] = field(default_factory=set)
    route: Optional[str] = None
    decision_id: Optional[str] = None
    provisional_result_id: Optional[str] = None
    pinned_refs: tuple[tuple[str, str, int], ...] = ()


class EdgeRuntimeCoordinator:
    """连接任务接入、感知、模型、调度上报及调度路径执行。"""

    def __init__(
        self,
        *,
        edge_node_id: str,
        ingress: EdgeTaskIngress,
        cache: EdgeValidationCache,
        perception: EdgePerception,
        pipeline: EdgeModelPipeline,
        scheduler: SchedulerReporter,
        summary_publisher: JsonPublisher,
        cloud_uploader: CloudPacketUploader,
        clock_ns=time.time_ns,
    ):
        self.edge_node_id = edge_node_id
        self.ingress = ingress
        self.cache = cache
        self.perception = perception
        self.pipeline = pipeline
        self.scheduler = scheduler
        self.summary_publisher = summary_publisher
        self.cloud_uploader = cloud_uploader
        self._clock_ns = clock_ns
        self._states: dict[tuple[str, str, str, str, str], _PacketState] = {}
        self._pending_analysis_reports: dict[
            tuple[str, str, str, str, str], dict[str, Any]
        ] = {}
        self._mutex = threading.Lock()
        self._last_task_activity_ns = 0
        self._model_versions: set[str] = set()
        self.pipeline.on_packet_completed = self.on_packet_completed

    def receive_raw_packet(self, raw_packet: dict[str, Any]) -> None:
        result = self.ingress.receive_packet(raw_packet)
        self._last_task_activity_ns = max(self._last_task_activity_ns, result.received_at_ns)
        if result.status != INGRESS_ACCEPTED or result.validated_packet is None:
            return
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
            return
        perceived = self.perception.perceive(downsampled.payload, context)
        if not perceived.status.success or perceived.payload is None:
            self._report_pre_model_failure(
                result.validated_packet,
                error_code=perceived.status.error_code or "PERCEPTION_FAILED",
                started_at_ns=result.received_at_ns,
            )
            return
        self.pipeline.ingest(raw_packet["sender_id"], perceived.payload)

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
        if task is None or task.dispatch_id is None:
            raise ValueError("completed packet has no active dispatch")
        raw_ref = packet.raw_packet_ref if packet is not None else None
        raw_uri = self.cache.raw_data_uri(raw_ref) if raw_ref is not None else None
        context_uri = self._context_uri(raw_ref) if raw_ref is not None else None
        report = PacketAnalysisReport.from_completion(
            completion,
            dispatch_id=task.dispatch_id,
            edge_node_id=self.edge_node_id,
            raw_data_ref=raw_uri,
            context_ref=context_uri,
        )
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
        state = _PacketState(report=report, completion=completion, raw_packet_ref=raw_ref)
        with self._mutex:
            packet_key = _key_from_completion(completion)
            self._states[packet_key] = state
        payload = report.as_dict()
        try:
            self.scheduler.report_analysis(payload)
        except Exception:
            with self._mutex:
                self._pending_analysis_reports[packet_key] = payload

    def handle_route_decision(self, decision: PacketRouteDecision) -> None:
        if decision.target_edge_node_id != self.edge_node_id:
            raise ValueError("TARGET_NODE_MISMATCH")
        key = _key_from_decision(decision)
        fingerprint = _fingerprint(decision.__dict__)
        result_status: Optional[str] = None
        publish_key: Optional[tuple[str, str]] = None
        with self._mutex:
            state = self._states.get(key)
            if state is None:
                raise ValueError("DECISION_TARGET_NOT_FOUND")
            previous = state.decision_fingerprints.get(decision.decision_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("DECISION_CONFLICT")
            else:
                if decision.dispatch_id != state.report.dispatch_id:
                    raise ValueError("DISPATCH_CONFLICT")
                if decision.route == DIRECT_FINAL_TO_SUMMARY:
                    if state.route is not None:
                        raise ValueError("INVALID_STATE_TRANSITION")
                elif decision.route == CLOUD_REVIEW_NOW:
                    if state.route is not None:
                        raise ValueError("INVALID_STATE_TRANSITION")
                    self._pin_locked(state)
                else:
                    if state.route not in (None, CLOUD_REVIEW_NOW):
                        raise ValueError("INVALID_STATE_TRANSITION")
                    self._pin_locked(state)
                state.route = decision.route
                state.decision_id = decision.decision_id
                state.decision_fingerprints[decision.decision_id] = fingerprint

            if decision.route == DIRECT_FINAL_TO_SUMMARY:
                result_status = RESULT_FINAL
            elif decision.route == EDGE_PROVISIONAL_AND_DEFER_CLOUD:
                result_status = RESULT_PROVISIONAL
            if result_status is not None:
                publish_key = (decision.decision_id, result_status)
                if publish_key in state.published_results:
                    return
                if publish_key in state.publishing_results:
                    raise ValueError("RESULT_PUBLISH_IN_PROGRESS")
                state.publishing_results.add(publish_key)

        if result_status is None or publish_key is None:
            return
        try:
            self._publish_edge_result(state, decision, result_status=result_status)
        except Exception:
            with self._mutex:
                state.publishing_results.discard(publish_key)
            raise
        with self._mutex:
            state.publishing_results.discard(publish_key)
            state.published_results.add(publish_key)

    def handle_cloud_instruction(self, instruction: CloudPacketReviewInstruction) -> None:
        key = (
            instruction.device_id,
            instruction.sender_id,
            instruction.task_id,
            instruction.bearing_id,
            instruction.packet_id,
        )
        fingerprint = _fingerprint(instruction.__dict__)
        upload_required = True
        with self._mutex:
            state = self._states.get(key)
            if state is None:
                raise ValueError("DECISION_TARGET_NOT_FOUND")
            if state.route not in (CLOUD_REVIEW_NOW, EDGE_PROVISIONAL_AND_DEFER_CLOUD):
                raise ValueError("INVALID_STATE_TRANSITION")
            if instruction.dispatch_id != state.report.dispatch_id:
                raise ValueError("DISPATCH_CONFLICT")
            if instruction.raw_data_ref != state.report.raw_data_ref:
                raise ValueError("DATA_REFERENCE_MISMATCH")
            previous = state.cloud_instruction_fingerprints.get(instruction.cloud_task_id)
            if previous is not None and previous != fingerprint:
                raise ValueError("CLOUD_INSTRUCTION_CONFLICT")
            if instruction.cloud_task_id in state.reported_cloud_tasks:
                return
            upload_required = instruction.cloud_task_id not in state.uploaded_cloud_tasks
            if upload_required and instruction.cloud_task_id in state.uploading_cloud_tasks:
                raise ValueError("CLOUD_UPLOAD_IN_PROGRESS")
            state.cloud_instruction_fingerprints[instruction.cloud_task_id] = fingerprint
            if upload_required:
                state.uploading_cloud_tasks.add(instruction.cloud_task_id)
        if upload_required:
            try:
                self.cloud_uploader.upload(instruction)
            except Exception as exc:
                with self._mutex:
                    state.uploading_cloud_tasks.discard(instruction.cloud_task_id)
                try:
                    self.scheduler.report_transfer_status({
                        "decision_id": instruction.decision_id,
                        "cloud_task_id": instruction.cloud_task_id,
                        "dispatch_id": instruction.dispatch_id,
                        "status": "FAILED",
                        "error": {
                            "code": "CLOUD_UPLOAD_FAILED",
                            "stage": "CLOUD_TRANSFER",
                            "retryable": True,
                            "message": str(exc),
                        },
                        "reported_at_ns": self._clock_ns(),
                    })
                except Exception:
                    pass
                raise
            with self._mutex:
                state.uploading_cloud_tasks.discard(instruction.cloud_task_id)
                state.uploaded_cloud_tasks.add(instruction.cloud_task_id)
                self._unpin_locked(state)
        self.scheduler.report_transfer_status({
            "decision_id": instruction.decision_id,
            "cloud_task_id": instruction.cloud_task_id,
            "dispatch_id": instruction.dispatch_id,
            "status": "UPLOADED",
            "error": None,
            "reported_at_ns": self._clock_ns(),
        })
        with self._mutex:
            state.reported_cloud_tasks.add(instruction.cloud_task_id)

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
        self._flush_analysis_reports()
        self.scheduler.report_status(self.node_status())

    def _flush_analysis_reports(self) -> None:
        with self._mutex:
            pending = list(self._pending_analysis_reports.items())
        for key, payload in pending:
            try:
                self.scheduler.report_analysis(payload)
            except Exception:
                continue
            with self._mutex:
                if self._pending_analysis_reports.get(key) is payload:
                    self._pending_analysis_reports.pop(key, None)

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

    def _context_uri(self, raw_ref: tuple[str, str, int]) -> Optional[str]:
        packet = self.cache.read(raw_ref)
        if packet is None:
            return None
        return self.cache.context_uri(
            device_id=packet["device_id"],
            bearing_id=packet["bearing_id"],
            sender_id=packet["sender_id"],
            anchor_packet_id=packet["packet_id"],
            anchor_end_generate_timestamp_ns=packet["end_generate_timestamp_ns"],
        )

    def _publish_edge_result(
        self, state: _PacketState, decision: PacketRouteDecision, *, result_status: str
    ) -> None:
        edge = state.completion.edge
        if edge is None:
            raise ValueError("cannot publish an edge result for a failed packet")
        result_id = _result_id(state.completion, 1)
        result = SummaryPacketResult(
            result_id=result_id,
            result_version=1,
            supersedes_result_id=None,
            dispatch_id=decision.dispatch_id,
            decision_id=decision.decision_id,
            device_id=decision.device_id,
            sender_id=decision.sender_id,
            task_id=decision.task_id,
            bearing_id=decision.bearing_id,
            packet_id=decision.packet_id,
            sequence_number=decision.sequence_number,
            result_status=result_status,
            decision_source="EDGE",
            review_status=(
                "NOT_REQUIRED" if result_status == RESULT_FINAL else "PENDING_CLOUD"
            ),
            edge=edge,
            action_level=action_level_for(edge.edge_result, edge.edge_risk_level),
        )
        self.summary_publisher.publish(result.as_dict())
        if result_status == RESULT_PROVISIONAL:
            state.provisional_result_id = result_id

    def _pin_locked(self, state: _PacketState) -> None:
        if state.pinned_refs:
            return
        if state.raw_packet_ref is None:
            raise ValueError("DATA_REFERENCE_NOT_FOUND")
        refs = [state.raw_packet_ref]
        if state.report.context_ref is not None:
            context = self.cache.read_context_uri(state.report.context_ref)
            for packet in context.get("packets", []):
                ref = (
                    packet["sender_id"],
                    packet["task_id"],
                    packet["sequence_number"],
                )
                if ref not in refs:
                    refs.append(ref)
        pinned: list[tuple[str, str, int]] = []
        for ref in refs:
            if self.cache.pin(ref):
                pinned.append(ref)
                continue
            for pinned_ref in pinned:
                self.cache.unpin(pinned_ref)
            raise ValueError("DATA_REFERENCE_NOT_FOUND")
        state.pinned_refs = tuple(pinned)

    def _unpin_locked(self, state: _PacketState) -> None:
        for ref in state.pinned_refs:
            self.cache.unpin(ref)
        state.pinned_refs = ()


def _key_from_completion(value: PacketExecutionCompleted) -> tuple[str, str, str, str, str]:
    return (value.device_id, value.sender_id, value.task_id, value.bearing_id, value.packet_id)


def _key_from_decision(value: PacketRouteDecision) -> tuple[str, str, str, str, str]:
    return (value.device_id, value.sender_id, value.task_id, value.bearing_id, value.packet_id)


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, default=list).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_id(value: PacketExecutionCompleted, version: int) -> str:
    return "packet_result_%s_%s_%s_v%d" % (
        value.task_id,
        value.bearing_id,
        value.packet_id,
        version,
    )
