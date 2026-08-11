# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

from edge_model.pipeline import EdgeModelPipeline
from edge_perception import EdgePerception
from edge_task_ingress import EdgeTaskIngress
from edge_validation_cache import EdgeValidationCache
from edge_aggregation import BearingAggregationWorkflow, HttpCloudReviewGateway

from .cloud import CloudPacketUploader
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


def build_edge_runtime(
    *,
    config: EdgeRuntimeConfig,
    ingress: EdgeTaskIngress,
    cache: EdgeValidationCache,
    perception: EdgePerception,
    pipeline: EdgeModelPipeline,
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
        analysis_path=config.scheduler.analysis_path,
        transfer_status_path=config.scheduler.transfer_status_path,
    )
    mqtt_ingress = MqttIngress(config.mqtt, lambda _: None)
    summary_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.summary_topic,
        qos=config.mqtt.qos,
    )
    device_result_publisher = MqttJsonPublisher(
        mqtt_ingress.client,
        topic=config.mqtt.device_result_topic,
        qos=config.mqtt.qos,
    )
    cloud_uploader = CloudPacketUploader(cache, config.cloud_node_urls)
    aggregation_workflow = None
    if config.cloud_node_urls:
        cloud_base_url = config.cloud_node_urls[sorted(config.cloud_node_urls)[0]]
        aggregation_workflow = BearingAggregationWorkflow(
            cache=cache,
            cloud=HttpCloudReviewGateway(cloud_base_url),
        )
    coordinator = EdgeRuntimeCoordinator(
        edge_node_id=config.edge_node_id,
        ingress=ingress,
        cache=cache,
        perception=perception,
        pipeline=pipeline,
        scheduler=scheduler,
        summary_publisher=summary_publisher,
        cloud_uploader=cloud_uploader,
        aggregation_workflow=aggregation_workflow,
        device_result_publisher=device_result_publisher,
    )
    mqtt_ingress.on_packet = coordinator.receive_raw_packet
    control_application = EdgeControlApplication(
        ingress,
        on_route_decision=coordinator.handle_route_decision,
        on_cloud_instruction=coordinator.handle_cloud_instruction,
    )
    heartbeat = HeartbeatLoop(
        config.scheduler.heartbeat_interval_seconds,
        coordinator.report_node_status,
    )
    service = EdgeRuntimeService(
        config=config,
        cache=cache,
        pipeline=pipeline,
        mqtt_ingress=mqtt_ingress,
        control_application=control_application,
        heartbeat=heartbeat,
    )
    return EdgeRuntimeAssembly(service=service, coordinator=coordinator)
