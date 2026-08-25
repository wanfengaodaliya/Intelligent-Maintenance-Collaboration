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

from .contracts import action_level_for
from .dispatcher import CompletionDispatcher
from .http import SchedulerReporter
from .suggestion_worker import SuggestionWorker
from .v12_flow import V12DecisionFlow


_DEVICE_DELIVERY_RECONCILIATION_INTERVAL_NS = 300_000_000_000
# AUD-11：聚合回补的最长等待时长。超龄的完成包在回补时直接出队放弃，
# 避免 v12 工作流长期不可用时无限重放（重放含 Scheduler 路由 HTTP 调用）。
_AGGREGATION_BACKFILL_MAX_AGE_NS = 60_000_000_000


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
        bearing_result_outbox: Any = None,
        on_local_bearing_result: Optional[Callable[[EdgeBearingResult], None]] = None,
        on_packet_route_error: Optional[Callable[[dict[str, Any]], None]] = None,
        suggestion_llm_client: Any = None,
        suggestion_publisher: Optional[JsonPublisher] = None,
        suggestion_outbox: Any = None,
        suggestion_history_window: int = 10,
        completion_dispatch_enabled: bool = True,
        completion_dispatch_queue_size: int = 256,
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
        self.bearing_result_outbox = bearing_result_outbox
        self.on_local_bearing_result = on_local_bearing_result or (lambda _: None)
        self.cloud_review_service: Any = None
        # 阶段 5：已发布 Outbox 记录保留期（纳秒）；None/<=0 表示禁用自动清理。
        # 由装配层按配置注入，维护轮次据此执行数据保留策略。
        self.outbox_published_retention_ns: int | None = None
        self.on_packet_route_error = on_packet_route_error or (lambda _: None)
        self.suggestion_llm_client = suggestion_llm_client
        self.suggestion_publisher = suggestion_publisher
        # 建议发布优先走持久化 Outbox（先落库后发送，避免断网/重启丢建议）；
        # 未装配 Outbox 时退回直接 MQTT 发布（向后兼容）。
        self.suggestion_outbox = suggestion_outbox
        self.suggestion_history_window = suggestion_history_window
        self._clock_ns = clock_ns
        self._mutex = threading.Lock()
        self._last_task_activity_ns = 0
        self._last_device_delivery_reconciliation_at_ns: int | None = None
        self._model_versions: set[str] = set()
        self._active_diagnosis_windows: dict[
            tuple[str, str, str, str, str, int], tuple[DiagnosisWindow, dict[str, Any]]
        ] = {}
        # AUD-11：v12 聚合暂不可用（如路由/落库瞬时失败）时登记的完成包，
        # 由维护轮次回补重放；key 与 _active_diagnosis_windows 同构。
        self._pending_aggregation: dict[
            tuple[str, str, str, str, str, int], PacketExecutionCompleted
        ] = {}
        # H1：完成事件分发线程——把路由/落盘/上报等控制面 I/O 移出数据面(推理 worker)。
        self.dispatcher = CompletionDispatcher(
            self._process_completion,
            enabled=completion_dispatch_enabled,
            queue_size=completion_dispatch_queue_size,
            on_error=self._report_dispatch_error,
        )
        # H3：设备级建议线程——设备级最终诊断结果触发一次 LLM，而非逐包。
        self.suggestion_worker = SuggestionWorker(
            llm_client=suggestion_llm_client,
            outbox=suggestion_outbox,
            publisher=suggestion_publisher,
            history_window=suggestion_history_window,
            clock_ns=clock_ns,
        )
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
        # H1：完成事件入队(O(1))；控制面逻辑由 CompletionDispatcher 线程异步执行。
        self.dispatcher.submit(completion)

    def _process_completion(self, completion: PacketExecutionCompleted) -> None:
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
        if self.v12_flow is not None and completion.edge is not None:
            try:
                if raw_packet is None:
                    raise ValueError("completed packet raw data is unavailable")
                edge_result = _edge_bearing_result(
                    completion, raw_packet, diagnosis_window=diagnosis_window
                )
                # Summary consumes the immutable local result. Publishing must not
                # depend on whether Scheduler can provide a later lifecycle route.
                self.on_local_bearing_result(edge_result)
                if route_decision is not None:
                    _, device = self.v12_flow.apply_edge_result(
                        edge_result,
                        route_decision,
                        expected_bearing_ids=task.expected_bearing_ids,
                        accepted_at_ns=self._clock_ns(),
                    )
                    self._capture_bearing_result(edge_result, route_decision, device)
            except Exception as error:
                self._report_v12_error(completion, error)
                # 聚合暂不可用：登记完成包等待维护轮回补重放（原 action 承诺的
                # raw_packet_retained_for_replay 由此闭环）。
                self._park_for_aggregation(completion)

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
                "action": "local_defer_fallback",
            }
            try:
                self.on_packet_route_error(record)
            except Exception:
                pass
            # Scheduler 路由请求失败（失联/超时）时不再作废该包：
            # 本地构造 DEFER 兜底决策，轴承结果按暂定落库并汇入设备轮次，
            # 轮次仍可正常闭合；原始窗口已由 PacketRoutingBridge 持久化，
            # 待 Scheduler 恢复后仍可按既有 DEFER 机制发起延迟云复核。
            return self._local_defer_fallback(completion, diagnosis_window)

    def _local_defer_fallback(
        self,
        completion: PacketExecutionCompleted,
        diagnosis_window: DiagnosisWindow | None = None,
    ) -> dict[str, Any]:
        """Build a local DEFER route decision when Scheduler is unreachable.

        Mirrors the Scheduler response contract consumed by
        BearingResultLifecycleManager.apply_route: identity fields must match
        the edge bearing result, and result_instruction must carry the
        PROVISIONAL / PENDING_CLOUD / degraded semantics of PacketRoute.DEFER.
        """
        start_sequence = (
            diagnosis_window.window_start_sequence
            if diagnosis_window is not None else completion.sequence_number
        )
        end_sequence = (
            diagnosis_window.window_end_sequence
            if diagnosis_window is not None else completion.sequence_number
        )
        decision_round_id = (
            diagnosis_window.decision_round_id
            if diagnosis_window is not None
            else build_decision_round_id(
                device_id=completion.device_id,
                task_id=completion.task_id,
                window_start_sequence=start_sequence,
                window_end_sequence=end_sequence,
            )
        )
        diagnosis_window_id = (
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
        return {
            "decision_id": None,
            "device_id": completion.device_id,
            "task_id": completion.task_id,
            "bearing_id": completion.bearing_id,
            "packet_id": completion.packet_id,
            "sequence_number": completion.sequence_number,
            "route": "DEFER",
            "legacy_route": "EDGE_PROVISIONAL_AND_DEFER_CLOUD",
            "needs_cloud_review": True,
            "deferred_cloud_review": True,
            "result_instruction": {
                "result_status": "PROVISIONAL",
                "decision_source": "EDGE",
                "review_status": "PENDING_CLOUD",
                "degraded": True,
            },
            "reason_codes": ["SCHEDULER_UNREACHABLE"],
            "defer_reason": "SCHEDULER_UNREACHABLE",
            "target": None,
            "created_at_ns": self._clock_ns(),
            "decision_round_id": decision_round_id,
            "diagnosis_window_id": diagnosis_window_id,
            "window_start_sequence": start_sequence,
            "window_end_sequence": end_sequence,
        }

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

    def submit_device_suggestion(self, result: Any) -> None:
        """H3：设备级最终诊断结果非阻塞入队，由建议线程消费生成一条建议。

        由 v12_flow 的设备级回调(_emit_device_result)统一触发，覆盖四种闭合路径：
        边缘自主闭合 / 轮次超时 / 云端复核回填 / 云仲裁修正。v12 关闭时无回调，
        自然满足"无设备级最终结果不触发 LLM"的硬约束。
        """
        if self.suggestion_worker is not None:
            self.suggestion_worker.submit(result)

    def _report_dispatch_error(self, completion: Any, error: Exception) -> None:
        """H1/H2：dispatcher 捕获到完成事件处理异常时上报，保留原异常语义。"""
        record = {
            "stage": "completion_dispatch",
            "device_id": getattr(completion, "device_id", None),
            "task_id": getattr(completion, "task_id", None),
            "bearing_id": getattr(completion, "bearing_id", None),
            "sender_id": getattr(completion, "sender_id", None),
            "packet_id": getattr(completion, "packet_id", None),
            "sequence_number": getattr(completion, "sequence_number", None),
            "error_code": getattr(error, "code", type(error).__name__),
            "message": str(error),
            "action": "completion_processing_failed",
        }
        try:
            self.on_packet_route_error(record)
        except Exception:
            pass

    def start_background(self) -> None:
        """启动完成分发线程与建议线程（由 service 在数据面启动后调用）。"""
        if self.dispatcher is not None:
            self.dispatcher.start()
        if self.suggestion_worker is not None:
            self.suggestion_worker.start()

    def stop_background(self) -> None:
        """停止完成分发线程与建议线程（dispatcher 先排空在途完成事件）。"""
        if self.dispatcher is not None:
            self.dispatcher.stop()
        if self.suggestion_worker is not None:
            self.suggestion_worker.stop()

    @property
    def completion_dispatcher_alive(self) -> bool:
        return self.dispatcher is None or self.dispatcher.alive

    @property
    def suggestion_worker_alive(self) -> bool:
        return self.suggestion_worker is None or self.suggestion_worker.alive

    @property
    def dispatch_overflow_total(self) -> int:
        return self.dispatcher.overflow_total if self.dispatcher is not None else 0

    @property
    def dispatch_queue_size(self) -> int:
        return self.dispatcher.queue_size if self.dispatcher is not None else 0

    @property
    def suggestion_queue_size(self) -> int:
        return self.suggestion_worker.queue_size if self.suggestion_worker is not None else 0

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
    def pending_aggregation_count(self) -> int:
        """等待聚合工作流回补的完成包数量（AUD-11 aggregation_waiting 分桶）。"""
        with self._mutex:
            return len(self._pending_aggregation)

    def _park_for_aggregation(self, completion: PacketExecutionCompleted) -> None:
        """v12 聚合暂不可用时登记完成包；同 identity 重复登记幂等覆盖。"""
        with self._mutex:
            self._pending_aggregation[_completion_identity(completion)] = completion

    def _flush_aggregation(self) -> int:
        """回补等待聚合的完成包，返回本轮出队数量。

        聚合工作流(v12_flow)未装配时直接出队（无可聚合去向）；
        装配后逐个重放完成处理，重放仍失败的包会经 park 重新入队，
        等待下一轮；超过回补最大等待时长的包出队放弃并上报。
        """
        with self._mutex:
            pending = list(self._pending_aggregation.values())
            self._pending_aggregation.clear()
        now = self._clock_ns()
        flushed = 0
        for completion in pending:
            flushed += 1
            age_ns = now - getattr(completion, "finished_at_ns", 0)
            if self.v12_flow is None or age_ns > _AGGREGATION_BACKFILL_MAX_AGE_NS:
                if self.v12_flow is not None:
                    self._report_aggregation_backfill_drop(completion)
                continue
            try:
                self._process_completion(completion)
            except Exception as error:
                self._report_dispatch_error(completion, error)
        return flushed

    def _report_aggregation_backfill_drop(self, completion: Any) -> None:
        """聚合回补超龄放弃时上报，保持丢弃可观测。"""
        record = {
            "stage": "aggregation_backfill",
            "device_id": getattr(completion, "device_id", None),
            "task_id": getattr(completion, "task_id", None),
            "bearing_id": getattr(completion, "bearing_id", None),
            "sender_id": getattr(completion, "sender_id", None),
            "packet_id": getattr(completion, "packet_id", None),
            "sequence_number": getattr(completion, "sequence_number", None),
            "error_code": "AGGREGATION_BACKFLUSH_EXPIRED",
            "message": "pending aggregation completion exceeded max backfill age",
            "action": "backfill_dropped_expired",
        }
        try:
            self.on_packet_route_error(record)
        except Exception:
            pass

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
        # 第一优先级：模型自带明确加载状态（如 H5 生产模型的 ready 标记）。
        # ready 仅在全部部署产物加载成功后为 True，未完成初始化的对象保持 False。
        if hasattr(fallback, "ready"):
            return "LOADED" if getattr(fallback, "ready") is True else "ERROR"
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
            "bearing_results_published": 0,
            "result_uploads": 0,
            "raw_sample_uploads": 0,
            "cloud_review_retries": 0,
            "outbox_published_cleaned": 0,
            "suggestions_published": 0,
            "device_delivery_results_checked": 0,
            "aggregation_backfilled": 0,
        }
        # AUD-11：先回补积压的完成包，再推进超时/发布，让回补结果
        # 赶在本轮轮次闭合与发布前进入聚合工作流。
        summary["aggregation_backfilled"] = self._flush_aggregation()
        if self.v12_flow is not None:
            promoted = self.v12_flow.promote_cloud_now_timeouts(
                now_ns=now, cloud_now_timeout_ns=self.cloud_now_timeout_ns
            )
            finalized = self.v12_flow.finalize_timeouts(
                now_ns=now, round_timeout_ns=self.round_timeout_ns
            )
            summary["provisional_promotions"] = len(promoted)
            summary["rounds_finalized"] = len(finalized)
            summary["device_delivery_results_checked"] = (
                self.reconcile_device_result_deliveries(now_ns=now)
            )
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
        if self.bearing_result_outbox is not None:
            summary["bearing_results_published"] = self.bearing_result_outbox.run_once(now)
            retention_ns = self.outbox_published_retention_ns
            if retention_ns is not None and retention_ns > 0:
                summary["outbox_published_cleaned"] += (
                    self.bearing_result_outbox.cleanup_published(
                        retention_ns=retention_ns, now_ns=now
                    )
                )
        if self.result_uploader is not None:
            summary["result_uploads"] = self.result_uploader.run_once(now)
        if self.suggestion_outbox is not None:
            summary["suggestions_published"] = self.suggestion_outbox.run_once(now)
        if self.raw_sample_uploader is not None:
            summary["raw_sample_uploads"] = self.raw_sample_uploader.run_once(now)
        if self.cloud_review_service is not None:
            summary["cloud_review_retries"] = self.cloud_review_service.retry_due(now)
        summary["finished_at_ns"] = self._clock_ns()
        return summary

    def reconcile_device_result_deliveries(
        self,
        *,
        now_ns: int | None = None,
        force: bool = False,
    ) -> int:
        """Run startup/full or periodic idempotent delivery reconciliation."""
        reconcile = getattr(
            self.v12_flow, "reconcile_device_result_deliveries", None
        )
        if not callable(reconcile):
            return 0
        now = self._clock_ns() if now_ns is None else now_ns
        last = self._last_device_delivery_reconciliation_at_ns
        if (
            not force
            and last is not None
            and now - last < _DEVICE_DELIVERY_RECONCILIATION_INTERVAL_NS
        ):
            return 0
        checked = int(reconcile())
        self._last_device_delivery_reconciliation_at_ns = now
        return checked

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
