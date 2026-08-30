# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional
from core.consistency_engine import ConsistencyPolicy

from common.control_auth import ControlAuthVerifier
from cloud_review import CloudReviewStore
from edge_model.pipeline import EdgeModelPipeline
from edge_task_ingress import EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache

from .config import EdgeRuntimeConfig
from .coordinator import EdgeRuntimeCoordinator
from .device_result_outbox import DeviceResultOutbox
from .maintenance import EdgeMaintenanceWorker
from .model_update_poller import ModelUpdatePoller
from .http import (
    EdgeControlApplication,
    HeartbeatLoop,
    JsonHttpClient,
    SchedulerReporter,
)
from .mqtt import MqttIngress, MqttJsonPublisher
from .service import EdgeRuntimeService
from .packet_route_reporter import PacketRouteReporter
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


@dataclass(frozen=True)
class EdgeRuntimeAssembly:
    service: EdgeRuntimeService
    coordinator: EdgeRuntimeCoordinator
    v12_flow: V12DecisionFlow | None
    bearing_result_outbox: DeviceResultOutbox | None = None
    device_result_outbox: DeviceResultOutbox | None = None
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
    control_auth_verifier: ControlAuthVerifier | None = None,
    consistency_policy: ConsistencyPolicy | None = None,
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
    mqtt_ingress = MqttIngress(config.mqtt, lambda _: None)
    bearing_result_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.bearing_result_topic,
        qos=config.mqtt.qos,
    )
    def _publish_bearing_result_with_identity(payload):
        return bearing_result_publisher.publish(
            with_trace_identity(
                payload,
                edge_node_id=config.edge_node_id,
                route_id=config.mqtt.bearing_result_topic,
            )
        )

    v12_flow = None
    bearing_result_outbox = None
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
            max_attempts=config.v12.result_upload_max_attempts,
        )
        bearing_result_outbox = DeviceResultOutbox(
            config.v12.database_path,
            _publish_bearing_result_with_identity,
            max_attempts=config.v12.device_result_publish_max_attempts,
            namespace="bearing_result",
        )
        def on_device_result_persist(result, connection):
            # Retain the Cloud history used by device-health analysis. This is
            # independent from formal cross-Edge conflict calculation and is no
            # longer published to summary/device-results.
            if not result_uploader.enqueue_device(result, connection=connection):
                raise ValueError("device result Cloud outbox payload conflict")

        v12_flow = V12DecisionFlow(
            BearingResultLifecycleManager(bearing_results),
            DeviceDecisionRoundRepository(config.v12.database_path),
            round_timeout_ns=config.v12.round_timeout_ms * 1_000_000,
            late_correction_retention_ns=config.v12.late_correction_retention_ms * 1_000_000,
            on_bearing_result=result_uploader.enqueue_bearing,
            on_device_result_persist=on_device_result_persist,
            on_manual_review=on_packet_route_error,
            consistency_policy=consistency_policy,
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
        bearing_result_outbox=bearing_result_outbox,
        on_local_bearing_result=(
            None
            if bearing_result_outbox is None
            else lambda result: _enqueue_local_bearing_result(
                bearing_result_outbox, result
            )
        ),
        on_packet_route_error=on_packet_route_error,
        completion_dispatch_enabled=config.completion_dispatch.enabled,
        completion_dispatch_queue_size=config.completion_dispatch.queue_size,
    )
    mqtt_ingress.on_packet = coordinator.receive_raw_packet
    if config.v12.enabled and config.v12.outbox_published_retention_hours > 0:
        # 阶段 5：注入已发布记录保留期，维护轮次自动执行数据保留策略。
        coordinator.outbox_published_retention_ns = (
            config.v12.outbox_published_retention_hours * 3_600 * 1_000_000_000
        )
    control_application = EdgeControlApplication(
        ingress,
        control_auth_verifier=control_auth_verifier,
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
        model_runtime = pipeline.model_client
        if not hasattr(model_runtime, "activate_version"):
            raise ValueError("model update poller requires local_h5 runtime")
        model_update_poller = ModelUpdatePoller(
            cloud_base_url=_cloud_result_base_url(config),
            edge_node_id=config.edge_node_id,
            model_root=config.model_update.model_root,
            model_runtime=model_runtime,
            signing_public_key_path=config.model_update.signing_public_key_path,
            expected_signing_key_id=config.model_update.signing_key_id,
            poll_interval_seconds=config.model_update.poll_interval_seconds,
            state_path=config.model_update.state_path,
            ca_file=config.model_update.ca_file,
            allow_insecure_http=config.model_update.allow_insecure_http,
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
        bearing_result_outbox=bearing_result_outbox,
        device_result_outbox=None,
        maintenance=maintenance,
    )


def _enqueue_local_bearing_result(
    outbox: DeviceResultOutbox, result: Any
) -> None:
    if not outbox.enqueue_bearing(result):
        raise ValueError(
            f"bearing result identity has conflicting payload: {result.result_id}"
        )
