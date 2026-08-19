# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping, Optional, Protocol

from core.bearing_actions import action_for_grade
from core.diagnosis_contracts import EdgeBearingResult
from core.diagnosis_identity import build_decision_round_id, build_diagnosis_window_id
from edge_model.contracts import PacketExecutionCompleted
from edge_model.pipeline import EdgeModelPipeline
from edge_task_ingress import EXPECTED_PACKET_COUNT, INGRESS_ACCEPTED, EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache
from diagnosis_window import DiagnosisWindow, DiagnosisWindowAssembler
from packet_routing_bridge import PacketRoutingBridge
from raw_sample_capture import RawSampleCaptureService
from raw_sample_capture.uploader import RawAnalysisSampleUploader
from result_uploader import ResultUploader

from suggestion_llm import SuggestionClient, build_suggestion_messages
from suggestion_rule import evaluate_suggestion

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
        pipeline: EdgeModelPipeline,
        scheduler: SchedulerReporter,
        packet_router: Optional[PacketRoutingBridge] = None,
        v12_flow: Optional[V12DecisionFlow] = None,
        raw_sample_capture: Optional[RawSampleCaptureService] = None,
        raw_sample_uploader: Optional[RawAnalysisSampleUploader] = None,
        result_uploader: Optional[ResultUploader] = None,
        diagnosis_window_assembler: Optional[DiagnosisWindowAssembler] = None,
        cloud_now_timeout_ns: int = 3_000_000_000,
        round_timeout_ns: int = 3_500_000_000,
        device_result_outbox: Any = None,
        on_packet_route_error: Optional[Callable[[dict[str, Any]], None]] = None,
        suggestion_llm_client: Optional[SuggestionClient] = None,
        suggestion_publisher: Optional[JsonPublisher] = None,
        suggestion_history_window: int = 10,
        clock_ns=time.time_ns,
    ):
        self.edge_node_id = edge_node_id
        self.ingress = ingress
        self.cache = cache
        self.pipeline = pipeline
        self.scheduler = scheduler
        self.packet_router = packet_router
        self.v12_flow = v12_flow
        self.raw_sample_capture = raw_sample_capture
        self.raw_sample_uploader = raw_sample_uploader
        self.result_uploader = result_uploader
        self.diagnosis_window_assembler = diagnosis_window_assembler
        self.cloud_now_timeout_ns = cloud_now_timeout_ns
        self.round_timeout_ns = round_timeout_ns
        self.device_result_outbox = device_result_outbox
        self.cloud_review_service: Any = None
        # 阶段 5：已发布 Outbox 记录保留期（纳秒）；None/<=0 表示禁用自动清理。
        # 由装配层按配置注入，维护轮次据此执行数据保留策略。
        self.outbox_published_retention_ns: int | None = None
        self.on_packet_route_error = on_packet_route_error or (lambda _: None)
        self.suggestion_llm_client = suggestion_llm_client
        self.suggestion_publisher = suggestion_publisher
        self.suggestion_history_window = suggestion_history_window
        # 设备级历史记录缓存：{device_id: [edge_result_dict, ...]}
        self._suggestion_history: dict[str, list[dict[str, Any]]] = {}
        self._clock_ns = clock_ns
        self._mutex = threading.Lock()
        self._last_task_activity_ns = 0
        self._model_versions: set[str] = set()
        self._active_diagnosis_windows: dict[
            tuple[str, str, str, str, str, int], tuple[DiagnosisWindow, dict[str, Any]]
        ] = {}
        self.pipeline.on_packet_completed = self.on_packet_completed

    def receive_raw_packet(self, raw_packet: dict[str, Any]) -> bool:
        result = self.ingress.receive_packet(raw_packet)
        self._last_task_activity_ns = max(self._last_task_activity_ns, result.received_at_ns)
        if result.status != INGRESS_ACCEPTED or result.validated_packet is None:
            return result.status != "REJECTED"
        if self.raw_sample_capture is not None:
            try:
                self.raw_sample_capture.record_packet(result.validated_packet)
            except Exception as error:
                self._report_raw_sample_error(result.validated_packet, error)
        packets = [result.validated_packet]
        if self.diagnosis_window_assembler is not None:
            window_packet = dict(result.validated_packet)
            end_ns = window_packet["end_generate_timestamp_ns"]
            window_packet["start_generate_timestamp_ns"] = end_ns - 50_000_000
            try:
                windows = self.diagnosis_window_assembler.append(window_packet)
            except Exception as error:
                self._report_raw_sample_error(window_packet, error)
                return True
            if not windows:
                if window_packet["sequence_number"] == EXPECTED_PACKET_COUNT:
                    self._close_incomplete_tail(window_packet)
                return True
            window = windows[0]
            merged = _merge_diagnosis_window(window)
            key = _completion_identity_from_packet(merged)
            with self._mutex:
                self._active_diagnosis_windows[key] = (window, merged)
            packets = [merged]
        for packet in packets:
            self._process_model_input(packet, result.received_at_ns)
        return True

    def _process_model_input(self, packet: dict[str, Any], received_at_ns: int) -> None:
        self.pipeline.ingest(packet["sender_id"], packet)

    def _close_incomplete_tail(self, packet: Mapping[str, Any]) -> None:
        if self.diagnosis_window_assembler is None:
            return
        report = self.diagnosis_window_assembler.finish_subject(
            device_id=packet["device_id"], task_id=packet["task_id"],
            bearing_id=packet["bearing_id"], sender_id=packet["sender_id"],
        )
        finished_at_ns = self._clock_ns()
        for sequence in report.incomplete_tail_sequences:
            self.ingress.record_packet_completion(
                device_id=report.device_id, sender_id=report.sender_id,
                task_id=report.task_id, bearing_id=report.bearing_id,
                sequence_number=sequence, output=None,
                error_code="INCOMPLETE_DIAGNOSIS_WINDOW", finished_at_ns=finished_at_ns,
            )
        try:
            self.on_packet_route_error({
                "stage": "diagnosis_window",
                "device_id": report.device_id, "task_id": report.task_id,
                "bearing_id": report.bearing_id, "sender_id": report.sender_id,
                "packet_ids": list(report.incomplete_tail_packet_ids),
                "sequence_numbers": list(report.incomplete_tail_sequences),
                "error_code": "INCOMPLETE_DIAGNOSIS_WINDOW",
                "action": "tail_recorded_without_diagnosis",
            })
        except Exception:
            pass

    def on_packet_completed(self, completion: PacketExecutionCompleted) -> None:
        with self._mutex:
            runtime_window = self._active_diagnosis_windows.pop(
                _completion_identity(completion), None
            )
        diagnosis_window = runtime_window[0] if runtime_window is not None else None
        runtime_raw_packet = runtime_window[1] if runtime_window is not None else None
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
        output = completion.edge.as_dict() if completion.edge is not None else None
        sequences = (
            range(diagnosis_window.window_start_sequence, diagnosis_window.window_end_sequence + 1)
            if diagnosis_window is not None else (completion.sequence_number,)
        )
        for sequence in sequences:
            self.ingress.record_packet_completion(
                device_id=completion.device_id,
                sender_id=completion.sender_id,
                task_id=completion.task_id,
                bearing_id=completion.bearing_id,
                sequence_number=sequence,
                output=output,
                error_code=completion.error_code,
                finished_at_ns=completion.finished_at_ns,
            )
        if completion.edge is not None:
            self._model_versions.add(completion.edge.model_version)
        raw_packet = runtime_raw_packet or (
            self.cache.read(raw_ref) if raw_ref is not None else None
        )
        route_decision = self._route_packet(completion, raw_packet, diagnosis_window)
        if self.v12_flow is not None and completion.edge is not None and route_decision is not None:
            try:
                if raw_packet is None:
                    raise ValueError("completed packet raw data is unavailable")
                edge_result = _edge_bearing_result(
                    completion, raw_packet, diagnosis_window=diagnosis_window
                )
                _, device = self.v12_flow.apply_edge_result(
                    edge_result,
                    route_decision,
                    expected_bearing_ids=task.expected_bearing_ids,
                    accepted_at_ns=self._clock_ns(),
                )
                self._capture_bearing_result(edge_result, route_decision, device)
                self._generate_suggestion(completion)
            except Exception as error:
                self._report_v12_error(completion, error)

    def _route_packet(
        self,
        completion: PacketExecutionCompleted,
        raw_packet: Mapping[str, Any] | None,
        diagnosis_window: DiagnosisWindow | None = None,
    ) -> dict[str, Any] | None:
        if self.packet_router is None:
            return None
        try:
            if raw_packet is None:
                raise ValueError("completed packet raw data is unavailable")
            return self.packet_router.route(
                raw_packet, completion, diagnosis_window=diagnosis_window
            )
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

    def _generate_suggestion(self, completion: PacketExecutionCompleted) -> None:
        """根据包完成结果生成建议并通过 MQTT 发布。

        流程：规则引擎决定建议类型 → LLM 翻译为自然语言 → MQTT 发布。
        LLM 不可用时自动降级为 fallback 文本。
        """
        if completion.edge is None:
            return
        if self.suggestion_publisher is None:
            return

        device_id = completion.device_id
        edge = completion.edge

        # 1. 更新历史记录
        with self._mutex:
            if device_id not in self._suggestion_history:
                self._suggestion_history[device_id] = []
            history = self._suggestion_history[device_id]
            history.append({
                "edge_result": edge.edge_result,
                "confidence": edge.confidence,
                "risk_level": edge.edge_risk_level,
            })
            # 只保留最近 N 条
            if len(history) > self.suggestion_history_window:
                history[:] = history[-self.suggestion_history_window:]

        # 2. 规则引擎决策
        rule_result = evaluate_suggestion(
            device_id=device_id,
            current_label=edge.edge_result,
            confidence=edge.confidence,
            risk_level=edge.edge_risk_level,
            history=list(history),
        )

        # 3. LLM 翻译为自然语言
        if self.suggestion_llm_client is not None:
            messages = build_suggestion_messages(
                device_id=device_id,
                label=edge.edge_result,
                confidence=edge.confidence,
                risk_level=edge.edge_risk_level,
                suggestion_type=rule_result.suggestion_type,
                trend=rule_result.trend,
            )
            llm_result = self.suggestion_llm_client.suggest(messages)
            suggestion_text = llm_result.text
        else:
            # 没有 LLM 时，用规则引擎的 reason 字段直接作为建议
            suggestion_text = f"{rule_result.reason}。"
            if rule_result.maintenance_window:
                suggestion_text = (
                    f"{rule_result.reason}，建议"
                    f"{'立即' if rule_result.maintenance_window == 'immediate' else rule_result.maintenance_window}检修。"
                )

        # 4. MQTT 发布
        try:
            self.suggestion_publisher.publish({
                "device_id": device_id,
                "task_id": completion.task_id,
                "bearing_id": completion.bearing_id,
                "packet_id": completion.packet_id,
                "suggestion": suggestion_text,
                "suggestion_type": rule_result.suggestion_type,
                "priority": rule_result.priority,
                "edge_result": edge.edge_result,
                "confidence": edge.confidence,
                "risk_level": edge.edge_risk_level,
            })
        except Exception:
            pass

    def node_status(self) -> dict[str, Any]:
        now = self._clock_ns()
        load_status = self._model_load_status()
        return {
            "edge_node_id": self.edge_node_id,
            "reported_at_ns": now,
            "health_status": "ONLINE",
            "queue_length": self.pipeline.queue_length,
            "models": [
                {"model_version": version, "model_load_status": load_status}
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

    @property
    def last_task_activity_ns(self) -> int:
        """最近一次任务活动时间（MQTT 与 HTTP 入口均会更新）。"""
        return self._last_task_activity_ns

    @property
    def model_load_status(self) -> str:
        return self._model_load_status()

    def _model_load_status(self) -> str:
        """根据模型真实加载生命周期推导状态，而不是固定 LOADED。"""
        if getattr(self.pipeline, "model_readiness", None) is not None:
            readiness = self.pipeline.model_readiness()
            if readiness.get("ok"):
                return "LOADED"
        fallback = getattr(self.pipeline, "fallback", None)
        if fallback is None:
            return "UNLOADED"
        if getattr(fallback, "deployment_status", None) == "built_in_rule":
            return "LOADED"
        if getattr(fallback, "estimator", None) is not None:
            return "LOADED"
        return "ERROR"

    def report_node_status(self) -> None:
        """只采集并上报节点状态，不推进任何业务状态机。"""
        self.scheduler.report_status(self.node_status())

    def run_maintenance_once(self, now_ns: int | None = None) -> dict[str, Any]:
        """执行一轮幂等的业务维护：超时推进、重试、恢复与待发布任务。"""
        now = self._clock_ns() if now_ns is None else now_ns
        summary: dict[str, Any] = {
            "finished_at_ns": 0,
            "provisional_promotions": 0,
            "rounds_finalized": 0,
            "device_results_published": 0,
            "result_uploads": 0,
            "raw_sample_uploads": 0,
            "cloud_review_retries": 0,
            "outbox_published_cleaned": 0,
        }
        if self.v12_flow is not None:
            promoted = self.v12_flow.promote_cloud_now_timeouts(
                now_ns=now, cloud_now_timeout_ns=self.cloud_now_timeout_ns
            )
            finalized = self.v12_flow.finalize_timeouts(
                now_ns=now, round_timeout_ns=self.round_timeout_ns
            )
            summary["provisional_promotions"] = len(promoted)
            summary["rounds_finalized"] = len(finalized)
        if self.device_result_outbox is not None:
            summary["device_results_published"] = self.device_result_outbox.run_once(now)
            retention_ns = self.outbox_published_retention_ns
            if retention_ns is not None and retention_ns > 0:
                # 阶段 5：数据保留策略在维护轮次内推进，只清理超期 PUBLISHED 记录。
                summary["outbox_published_cleaned"] = (
                    self.device_result_outbox.cleanup_published(
                        retention_ns=retention_ns, now_ns=now
                    )
                )
        if self.result_uploader is not None:
            summary["result_uploads"] = self.result_uploader.run_once(now)
        if self.raw_sample_uploader is not None:
            summary["raw_sample_uploads"] = self.raw_sample_uploader.run_once(now)
        if self.cloud_review_service is not None:
            summary["cloud_review_retries"] = self.cloud_review_service.retry_due(now)
        summary["finished_at_ns"] = self._clock_ns()
        return summary

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


def _edge_bearing_result(
    completion: PacketExecutionCompleted, raw_packet: Mapping[str, Any],
    *, diagnosis_window: DiagnosisWindow | None = None,
) -> EdgeBearingResult:
    if completion.edge is None:
        raise ValueError("edge bearing result requires a successful edge model output")
    end_ns = raw_packet.get("end_generate_timestamp_ns")
    if isinstance(end_ns, bool) or not isinstance(end_ns, int) or end_ns <= 0:
        raise ValueError("raw packet requires end_generate_timestamp_ns")
    start_sequence = (
        diagnosis_window.window_start_sequence
        if diagnosis_window is not None else completion.sequence_number
    )
    end_sequence = (
        diagnosis_window.window_end_sequence
        if diagnosis_window is not None else completion.sequence_number
    )
    start_ns = (
        diagnosis_window.window_start_ns
        if diagnosis_window is not None else max(0, end_ns - 50_000_000)
    )
    window_id = (
        diagnosis_window.diagnosis_window_id
        if diagnosis_window is not None
        else build_diagnosis_window_id(
            device_id=completion.device_id,
            task_id=completion.task_id,
            bearing_id=completion.bearing_id,
            sender_id=completion.sender_id,
            window_start_sequence=start_sequence,
            window_end_sequence=end_sequence,
        )
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
        decision_round_id=(
            diagnosis_window.decision_round_id
            if diagnosis_window is not None
            else build_decision_round_id(
                device_id=completion.device_id,
                task_id=completion.task_id,
                window_start_sequence=start_sequence,
                window_end_sequence=end_sequence,
            )
        ),
        diagnosis_window_id=window_id,
        window_start_sequence=start_sequence,
        window_end_sequence=end_sequence,
        window_start_ns=start_ns,
        window_end_ns=end_ns,
        contributing_packet_ids=(
            diagnosis_window.contributing_packet_ids
            if diagnosis_window is not None else (completion.packet_id,)
        ),
        bearing_state=completion.edge.edge_result,
        confidence=completion.edge.confidence,
        data_quality_score=completion.data_quality_score,
        risk_level=completion.edge.edge_risk_level,
        action_grade=action_grade,
        recommended_action=action_for_grade(action_grade),
        model_version=completion.edge.model_version,
        created_at_ns=completion.finished_at_ns,
        diagnosis_label=completion.edge.diagnosis_label,
        class_probabilities=completion.edge.class_probabilities,
    )


def _merge_diagnosis_window(window: DiagnosisWindow) -> dict[str, Any]:
    first, last = window.packets[0], window.packets[-1]
    data: dict[str, Any] = {}
    for channel, source in first["data"].items():
        if not isinstance(source, Mapping):
            data[channel] = last["data"][channel]
            continue
        merged = dict(source)
        values: list[Any] = []
        for packet in window.packets:
            current = packet["data"][channel]
            if not isinstance(current, Mapping) or "values" not in current:
                raise ValueError(f"diagnosis window channel {channel} has no values")
            values.extend(list(current["values"]))
        merged["values"] = values
        merged["sample_count"] = len(values)
        data[channel] = merged
    vibration = data.get("vibration") if isinstance(data.get("vibration"), Mapping) else {}
    return {
        "device_id": window.device_id,
        "task_id": window.task_id,
        "bearing_id": window.bearing_id,
        "sender_id": window.sender_id,
        "packet_id": last["packet_id"],
        "sequence_number": window.window_end_sequence,
        "start_generate_timestamp_ns": window.window_start_ns,
        "end_generate_timestamp_ns": window.window_end_ns,
        "start_timestamp_ns": window.window_start_ns,
        "end_timestamp_ns": window.window_end_ns,
        "window_start_ns": window.window_start_ns,
        "window_end_ns": window.window_end_ns,
        "window_start_sequence": window.window_start_sequence,
        "window_end_sequence": window.window_end_sequence,
        "contributing_packet_ids": list(window.contributing_packet_ids),
        "diagnosis_window_id": window.diagnosis_window_id,
        "decision_round_id": window.decision_round_id,
        "sample_rate_hz": vibration.get("sample_rate_hz"),
        "sample_count": vibration.get("sample_count"),
        "data": data,
    }


def _completion_identity(value: PacketExecutionCompleted) -> tuple[str, str, str, str, str, int]:
    return (
        value.device_id, value.sender_id, value.task_id, value.bearing_id,
        value.packet_id, value.sequence_number,
    )


def _completion_identity_from_packet(value: Mapping[str, Any]) -> tuple[str, str, str, str, str, int]:
    return tuple(
        value[field]
        for field in (
            "device_id", "sender_id", "task_id", "bearing_id", "packet_id", "sequence_number",
        )
    )
