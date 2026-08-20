# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from cloud_review import CloudReviewStore
from edge_model.pipeline import EdgeModelPipeline
from edge_task_ingress import EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache

from .config import EdgeRuntimeConfig
from .coordinator import EdgeRuntimeCoordinator
from .device_result_outbox import DeviceResultOutbox
from .maintenance import EdgeMaintenanceWorker
from .model_update_poller import ModelUpdatePoller
from .suggestion_outbox import SuggestionOutbox
from .http import (
    EdgeControlApplication,
    HeartbeatLoop,
    JsonHttpClient,
    SchedulerReporter,
)
from .mqtt import MqttIngress, MqttJsonPublisher
from .service import EdgeRuntimeService
from .packet_route_reporter import DeviceArbitrationReporter, PacketRouteReporter
from .trace_identity import with_trace_identity
from packet_routing_bridge import PacketRoutingBridge
from device_decision import DeviceDecisionRoundRepository
from result_lifecycle import BearingResultLifecycleManager, BearingResultRepository
from result_uploader import ResultUploader
from raw_sample_capture import (
    RawSampleCapturePolicy,
    RawSampleCaptureService,
    RawSampleFreezer,
    RawSampleRepository,
    HttpRawSampleTransport,
    RawAnalysisSampleUploader,
)
from diagnosis_window import DiagnosisWindowAssembler
from .v12_flow import V12DecisionFlow
from suggestion_llm import SuggestionClient


@dataclass(frozen=True)
class EdgeRuntimeAssembly:
    service: EdgeRuntimeService
    coordinator: EdgeRuntimeCoordinator
    v12_flow: V12DecisionFlow | None
    device_result_outbox: DeviceResultOutbox | None = None
    suggestion_outbox: SuggestionOutbox | None = None
    maintenance: EdgeMaintenanceWorker | None = None


def _cloud_result_base_url(config: EdgeRuntimeConfig) -> str:
    """Resolve the cloud_service base URL for edge outbound HTTP calls.

    Pick the first node of cloud_node_urls (shared by V1.2 result uploads,
    raw-sample uploads and the model-update poller). from_env() guarantees a
    non-empty mapping by falling back to CLOUD_SERVICE_BASE_URL; URLs are
    validated to be HTTP(S) by EdgeRuntimeConfig.validate().
    """
    return config.cloud_node_urls[sorted(config.cloud_node_urls)[0]]



def build_edge_runtime(
    *,
    config: EdgeRuntimeConfig,
    ingress: EdgeTaskIngress,
    cache: EdgeValidationCache,
    pipeline: EdgeModelPipeline,
    cloud_review_store: Optional[CloudReviewStore] = None,
    on_packet_route_error: Optional[Callable[[dict], None]] = None,
    enable_heartbeat: bool = True,
) -> EdgeRuntimeAssembly:
    """把现有边缘计算模块装配为可启动的协议运行时。"""
    errors = config.validate()
    if errors:
        raise ValueError("边缘运行时配置无效: " + "; ".join(errors))
    if ingress.config.edge_node_id != config.edge_node_id:
        raise ValueError("任务接入 edge_node_id 与运行时配置不一致")

    scheduler = SchedulerReporter(
        JsonHttpClient(
            config.scheduler.base_url,
            timeout_seconds=config.scheduler.request_timeout_seconds,
        ),
        status_path=config.scheduler.status_path,
    )
    scheduler_client = scheduler.client
    packet_route_reporter = PacketRouteReporter(scheduler_client.post)
    device_arbitration_reporter = DeviceArbitrationReporter(scheduler_client.post)
    mqtt_ingress = MqttIngress(config.mqtt, lambda _: None)
    device_result_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.device_result_topic,
        qos=config.mqtt.qos,
    )
    suggestion_client = (
        SuggestionClient(
            base_url=config.suggestion_llm.base_url,
            timeout_seconds=config.suggestion_llm.timeout_seconds,
            fallback_text=config.suggestion_llm.fallback_text,
        )
        if config.suggestion_llm.enabled
        else None
    )
    suggestion_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.suggestion_topic,
        qos=config.mqtt.qos,
    )
    def _publish_device_result_with_identity(payload):
        # 阶段 4：设备级结果发布统一携带 trace 身份字段，
        # route_id 使用设备结果主题，便于跨模块按链路追踪。
        return device_result_publisher.publish(
            with_trace_identity(
                payload,
                edge_node_id=config.edge_node_id,
                route_id=config.mqtt.device_result_topic,
            )
        )

    def _enrich_uploaded_payload(payload, route_id: str):
        # 阶段 4：轴承/设备结果上报 Cloud 时统一携带 trace 身份字段。
        return with_trace_identity(
            payload,
            edge_node_id=config.edge_node_id,
            route_id=route_id,
        )

    def _publish_suggestion_with_identity(payload):
        # 建议发布统一携带 trace 身份字段，route_id 使用建议主题，
        # 便于跨模块按链路追踪。
        return suggestion_publisher.publish(
            with_trace_identity(
                payload,
                edge_node_id=config.edge_node_id,
                route_id=config.mqtt.suggestion_topic,
            )
        )

    suggestion_outbox = SuggestionOutbox(
        config.v12.database_path,
        _publish_suggestion_with_identity,
        max_attempts=config.v12.suggestion_publish_max_attempts,
    )
    v12_flow = None
    device_result_outbox = None
    raw_sample_capture = None
    raw_sample_uploader = None
    result_uploader = None
    if config.raw_sample_capture.enabled:
        raw_config = config.raw_sample_capture
        raw_sample_capture = RawSampleCaptureService(
            RawSampleCapturePolicy(
                history_window_ms=raw_config.history_window_ms,
                normal_sample_interval_seconds=raw_config.normal_sample_interval_seconds,
            ),
            RawSampleFreezer(),
            RawSampleRepository(
                raw_config.directory,
                max_storage_bytes=raw_config.max_local_storage_mb * 1024 * 1024,
                retention_ns=raw_config.local_retention_hours * 60 * 60 * 1_000_000_000,
                max_upload_attempts=raw_config.max_upload_attempts,
            ),
        )
        raw_sample_uploader = RawAnalysisSampleUploader(
            raw_sample_capture.repository,
            HttpRawSampleTransport(
                _cloud_result_base_url(config),
                timeout_seconds=config.scheduler.request_timeout_seconds,
            ).upload,
            batch_size=raw_config.upload_batch_size,
        )
    if config.v12.enabled:
        bearing_results = BearingResultRepository(config.v12.database_path)
        # V1.2 轴承/设备结果的目标端点是 cloud_service 的 /cloud/* 路由，
        # 必须用 Cloud 客户端直发。此前误注入 scheduler_client.post：
        # Scheduler 没有这些路由，直连部署时上报全部 404，
        # 重试耗尽后进死信，云端永远收不到边缘结论。
        result_uploader = ResultUploader(
            config.v12.database_path,
            JsonHttpClient(
                _cloud_result_base_url(config),
                timeout_seconds=config.scheduler.request_timeout_seconds,
            ).post,
            payload_enricher=_enrich_uploaded_payload,
            max_attempts=config.v12.result_upload_max_attempts,
        )
        device_result_outbox = DeviceResultOutbox(
            config.v12.database_path,
            _publish_device_result_with_identity,
            max_attempts=config.v12.device_result_publish_max_attempts,
        )
        # coordinator 在 v12_flow 之后才构造，用 holder 承接引用供 on_device_result 使用。
        coordinator_holder: list[EdgeRuntimeCoordinator] = []

        def on_device_result(result):
            # 设备级结果双通道发布（职责不同，缺一不可）：
            #   通道 A（MQTT device_result_topic）：实时广播给在线订阅方
            #     （大屏/前端/其他节点），先落 DeviceResultOutbox，由维护
            #     轮次后台发送，至少一次送达。
            #   通道 B（HTTP /cloud/device-decision-results）：权威持久化
            #     上报 cloud_service，先落 ResultUploader 队列，重试耗尽
            #     进死信；云端以 {"status": "accepted"/"duplicate"} 回应。
            # 两通道均以 result_id 为主键幂等入队：重复入队同载荷跳过，
            # 上传侧同 result_id 不同载荷标 CONFLICT；云端 duplicate 响应
            # 表明服务端亦幂等，断线重试不会产生重复记录。
            device_result_outbox.enqueue(result)
            try:
                result_uploader.enqueue_device(result)
            except Exception:
                pass
            # H3：设备级最终结果触发一次建议（覆盖四种闭合路径），非阻塞入队。
            if coordinator_holder:
                coordinator_holder[0].submit_device_suggestion(result)
            if raw_sample_capture is not None and result.has_conflict:
                for bearing in bearing_results.list_current_round(
                    result.device_id, result.task_id, result.decision_round_id
                ):
                    try:
                        raw_sample_capture.capture(
                            {
                                "device_id": bearing.device_id,
                                "task_id": bearing.task_id,
                                "bearing_id": bearing.bearing_id,
                                "sender_id": bearing.sender_id,
                                "decision_round_id": bearing.decision_round_id,
                                "device_conflict": True,
                                "edge_model_version": bearing.model_version,
                                "cloud_corrected": bearing.lifecycle_state.value == "LATE_CLOUD_CORRECTED",
                                "created_at_ns": bearing.created_at_ns,
                            }
                        )
                    except Exception:
                        continue

        v12_flow = V12DecisionFlow(
            BearingResultLifecycleManager(bearing_results),
            DeviceDecisionRoundRepository(config.v12.database_path),
            round_timeout_ns=config.v12.round_timeout_ms * 1_000_000,
            late_correction_retention_ns=config.v12.late_correction_retention_ms * 1_000_000,
            on_bearing_result=result_uploader.enqueue_bearing,
            on_device_result=on_device_result,
            on_device_conflict=lambda payload: device_arbitration_reporter.report(
                {**payload, "edge_node_id": config.edge_node_id}
            ),
            on_manual_review=on_packet_route_error,
        )
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id=config.edge_node_id,
        ingress=ingress,
        cache=cache,
        pipeline=pipeline,
        scheduler=scheduler,
        packet_router=(
            PacketRoutingBridge(
                edge_node_id=config.edge_node_id,
                store=cloud_review_store,
                post=lambda _, payload: packet_route_reporter.report(payload),
            )
            if cloud_review_store is not None
            else None
        ),
        v12_flow=v12_flow,
        raw_sample_capture=raw_sample_capture,
        raw_sample_uploader=raw_sample_uploader,
        result_uploader=result_uploader,
        diagnosis_window_assembler=(
            DiagnosisWindowAssembler(
                window_ms=config.v12.diagnosis_window_ms,
                step_ms=config.v12.diagnosis_step_ms,
                overlap_enabled=config.v12.diagnosis_overlap_enabled,
            )
            if config.v12.enabled else None
        ),
        cloud_now_timeout_ns=config.v12.cloud_now_timeout_ms * 1_000_000,
        round_timeout_ns=config.v12.round_timeout_ms * 1_000_000,
        device_result_outbox=device_result_outbox,
        on_packet_route_error=on_packet_route_error,
        suggestion_llm_client=suggestion_client,
        suggestion_publisher=suggestion_publisher,
        suggestion_outbox=suggestion_outbox,
        suggestion_history_window=config.suggestion_llm.history_window,
        completion_dispatch_enabled=config.completion_dispatch.enabled,
        completion_dispatch_queue_size=config.completion_dispatch.queue_size,
    )
    coordinator_holder.append(coordinator)
    mqtt_ingress.on_packet = coordinator.receive_raw_packet
    if config.v12.enabled and config.v12.outbox_published_retention_hours > 0:
        # 阶段 5：注入已发布记录保留期，维护轮次自动执行数据保留策略。
        coordinator.outbox_published_retention_ns = (
            config.v12.outbox_published_retention_hours * 3_600 * 1_000_000_000
        )
    control_application = EdgeControlApplication(
        ingress,
        on_device_arbitration_result=(
            None
            if v12_flow is None
            else lambda payload: v12_flow.apply_cloud_arbitration_result(
                payload, accepted_at_ns=time.time_ns()
            )
        ),
    )
    heartbeat = (
        HeartbeatLoop(
            config.scheduler.heartbeat_interval_seconds,
            coordinator.report_node_status,
        )
        if enable_heartbeat
        else None
    )
    maintenance = (
        EdgeMaintenanceWorker(
            coordinator.run_maintenance_once,
            interval_seconds=config.maintenance.interval_seconds,
        )
        if config.maintenance.enabled
        else None
    )
    model_update_poller = None
    if config.model_update.enabled:
        from edge_model.version_store import DEFAULT_MODEL_ROOT

        model_update_poller = ModelUpdatePoller(
            cloud_base_url=_cloud_result_base_url(config),
            edge_node_id=config.edge_node_id,
            model_root=config.model_update.model_root or DEFAULT_MODEL_ROOT,
            poll_interval_seconds=config.model_update.poll_interval_seconds,
            state_path=config.model_update.state_path,
        )
    service = EdgeRuntimeService(
        config=config,
        cache=cache,
        pipeline=pipeline,
        mqtt_ingress=mqtt_ingress,
        control_application=control_application,
        heartbeat=heartbeat,
        maintenance=maintenance,
        model_update_poller=model_update_poller,
        coordinator=coordinator,
    )
    return EdgeRuntimeAssembly(
        service=service,
        coordinator=coordinator,
        v12_flow=v12_flow,
        device_result_outbox=device_result_outbox,
        suggestion_outbox=suggestion_outbox,
        maintenance=maintenance,
    )
