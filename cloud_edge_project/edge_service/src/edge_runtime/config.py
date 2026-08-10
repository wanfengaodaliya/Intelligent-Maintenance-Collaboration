# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


_EDGE_NODE_ID = re.compile(r"^edge_\d{2,}$")


@dataclass(frozen=True)
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    keepalive_seconds: int = 30
    qos: int = 1
    input_topic: str = "edge/edge_01/input"
    summary_topic: str = "summary/packet-results"
    client_id: str = "edge_01-runtime"
    ingress_queue_capacity: int = 160


@dataclass(frozen=True)
class SchedulerConfig:
    base_url: str = "http://127.0.0.1:8003"
    status_path: str = "/scheduler/edge-status"
    analysis_path: str = "/scheduler/packet-analysis"
    transfer_status_path: str = "/scheduler/cloud-transfer-status"
    request_timeout_seconds: float = 2.0
    heartbeat_interval_seconds: float = 1.0


@dataclass(frozen=True)
class ControlServerConfig:
    host: str = "0.0.0.0"
    port: int = 8011


@dataclass(frozen=True)
class EdgeRuntimeConfig:
    edge_node_id: str = "edge_01"
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    control: ControlServerConfig = field(default_factory=ControlServerConfig)
    cloud_node_urls: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not _EDGE_NODE_ID.fullmatch(self.edge_node_id):
            errors.append("edge_node_id 必须匹配 edge_<至少两位数字>")
        expected_topic = f"edge/{self.edge_node_id}/input"
        if self.mqtt.input_topic != expected_topic:
            errors.append(f"mqtt.input_topic 必须等于 {expected_topic}")
        if not isinstance(self.mqtt.port, int) or isinstance(self.mqtt.port, bool) \
                or not 1 <= self.mqtt.port <= 65535:
            errors.append("mqtt.port 必须是有效端口")
        if self.mqtt.qos != 1:
            errors.append("边缘 MQTT 契约要求 qos=1")
        if self.mqtt.ingress_queue_capacity < 1:
            errors.append("mqtt.ingress_queue_capacity 必须是正整数")
        if not self.mqtt.host.strip() or not self.mqtt.client_id.strip():
            errors.append("MQTT host 和 client_id 不能为空")
        if not self.mqtt.summary_topic.strip():
            errors.append("mqtt.summary_topic 不能为空")
        if not self.scheduler.base_url.startswith(("http://", "https://")):
            errors.append("scheduler.base_url 必须使用 HTTP 或 HTTPS")
        if self.scheduler.heartbeat_interval_seconds != 1.0:
            errors.append("heartbeat_interval_seconds 必须固定为1秒")
        if self.scheduler.request_timeout_seconds <= 0:
            errors.append("scheduler.request_timeout_seconds 必须为正数")
        if not isinstance(self.control.port, int) or isinstance(self.control.port, bool) \
                or not 1 <= self.control.port <= 65535:
            errors.append("control.port 必须是有效端口")
        if not self.control.host.strip():
            errors.append("control.host 不能为空")
        if any(
            not isinstance(node_id, str) or not node_id.strip()
            or not isinstance(url, str) or not url.startswith(("http://", "https://"))
            for node_id, url in self.cloud_node_urls.items()
        ):
            errors.append("cloud_node_urls 必须是节点ID到HTTP(S)地址的映射")
        return errors
