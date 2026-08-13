# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class EdgeRuntimeAssembly:
    service: EdgeRuntimeService
    coordinator: EdgeRuntimeCoordinator
    window_review_store: WindowReviewStore


def build_edge_runtime(
    *,
    config: EdgeRuntimeConfig,
    ingress: EdgeTaskIngress,
    cache: EdgeValidationCache,
    perception: EdgePerception,
    pipeline: EdgeModelPipeline,
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
    mqtt_ingress = MqttIngress(config.mqtt, lambda _: None)
    device_result_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.device_result_topic,
        qos=config.mqtt.qos,
    )
    transfer = config.window_transfer
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
    aggregation_workflow = None
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
    )
    mqtt_ingress.on_packet = coordinator.receive_raw_packet
    control_application = EdgeControlApplication(ingress)
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
    )
