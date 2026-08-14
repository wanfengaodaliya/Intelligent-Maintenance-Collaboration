# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from cloud_review import CloudReviewStore
from edge_model.pipeline import EdgeModelPipeline
from edge_perception import EdgePerception
from edge_task_ingress import EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache
from edge_aggregation import (
    BearingAggregationWorkflow,
    DurableWindowReviewGateway,
    HttpCloudReviewGateway,
    WindowReviewDispatcher,
    WindowReviewHttpClient,
    WindowReviewStore,
)

from .config import EdgeRuntimeConfig
from .coordinator import EdgeRuntimeCoordinator
from .http import (
    EdgeControlApplication,
    HeartbeatLoop,
    JsonHttpClient,
    SchedulerReporter,
)
from .mqtt import MqttIngress, MqttJsonPublisher
from .service import EdgeRuntimeService
from .packet_route_reporter import DeviceArbitrationReporter, PacketRouteReporter
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
    window_review_store: WindowReviewStore | None
    v12_flow: V12DecisionFlow | None


def build_edge_runtime(
    *,
    config: EdgeRuntimeConfig,
    ingress: EdgeTaskIngress,
    cache: EdgeValidationCache,
    perception: EdgePerception,
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
    transfer = config.window_transfer
    window_review_store = None
    dispatcher = None
    aggregation_workflow = None
    if config.v12.legacy_realtime_aggregation:
        window_review_store = WindowReviewStore(
            transfer.cache_directory,
            hard_limit_bytes=transfer.hard_limit_bytes,
            warning_bytes=transfer.warning_bytes,
            reserved_free_bytes=transfer.reserved_free_bytes,
        )
        dispatcher = WindowReviewDispatcher(
            window_review_store,
            WindowReviewHttpClient(transfer.cloud_base_url),
            interval_seconds=transfer.dispatch_interval_seconds,
        )
        if config.cloud_node_urls:
            cloud_base_url = config.cloud_node_urls[sorted(config.cloud_node_urls)[0]]
            aggregation_workflow = BearingAggregationWorkflow(
                cache=cache,
                cloud=DurableWindowReviewGateway(
                    HttpCloudReviewGateway(cloud_base_url), window_review_store
                ),
                packet_cloud_confidence_threshold=(
                    transfer.packet_cloud_confidence_threshold
                ),
            )
    v12_flow = None
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
            ),
        )
        raw_sample_uploader = RawAnalysisSampleUploader(
            raw_sample_capture.repository,
            HttpRawSampleTransport(
                config.window_transfer.cloud_base_url,
                timeout_seconds=config.scheduler.request_timeout_seconds,
            ).upload,
            batch_size=raw_config.upload_batch_size,
        )
    if config.v12.enabled:
        bearing_results = BearingResultRepository(config.v12.database_path)
        result_uploader = ResultUploader(config.v12.database_path, scheduler_client.post)

        def on_device_result(result):
            device_result_publisher.publish(result.as_dict())
            try:
                result_uploader.enqueue_device(result)
            except Exception:
                pass
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
            on_bearing_result=result_uploader.enqueue_bearing,
            on_device_result=on_device_result,
            on_device_conflict=lambda payload: device_arbitration_reporter.report(
                {**payload, "edge_node_id": config.edge_node_id}
            ),
        )
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id=config.edge_node_id,
        ingress=ingress,
        cache=cache,
        perception=perception,
        pipeline=pipeline,
        scheduler=scheduler,
        aggregation_workflow=aggregation_workflow,
        device_result_publisher=device_result_publisher,
        window_review_store=window_review_store,
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
        legacy_realtime_aggregation=config.v12.legacy_realtime_aggregation,
        cloud_now_timeout_ns=config.v12.cloud_now_timeout_ms * 1_000_000,
        round_timeout_ns=config.v12.round_timeout_ms * 1_000_000,
        on_packet_route_error=on_packet_route_error,
    )
    mqtt_ingress.on_packet = coordinator.receive_raw_packet
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
    service = EdgeRuntimeService(
        config=config,
        cache=cache,
        pipeline=pipeline,
        mqtt_ingress=mqtt_ingress,
        control_application=control_application,
        heartbeat=heartbeat,
        window_dispatcher=dispatcher,
    )
    return EdgeRuntimeAssembly(
        service=service,
        coordinator=coordinator,
        window_review_store=window_review_store,
        v12_flow=v12_flow,
    )
