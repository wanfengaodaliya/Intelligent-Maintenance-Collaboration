# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Mapping, Optional

from edge_validation_cache import EdgeValidationCache

from .contracts import CloudPacketReviewInstruction
from .http import JsonHttpClient


class CloudUploadError(RuntimeError):
    pass


class CloudPacketUploader:
    """按调度指令从边缘缓存读取数据并直接发送到目标云节点。"""

    def __init__(
        self,
        cache: EdgeValidationCache,
        cloud_node_urls: Mapping[str, str],
        *,
        timeout_seconds: float = 5.0,
    ):
        self.cache = cache
        self.cloud_node_urls = dict(cloud_node_urls)
        self.timeout_seconds = timeout_seconds

    def upload(self, instruction: CloudPacketReviewInstruction) -> dict[str, Any]:
        base_url = self.cloud_node_urls.get(instruction.cloud_node_id)
        if base_url is None:
            raise CloudUploadError("unknown cloud_node_id: %s" % instruction.cloud_node_id)
        raw_packet = self.cache.read_uri(instruction.raw_data_ref)
        if raw_packet is None:
            raise CloudUploadError("DATA_REFERENCE_NOT_FOUND")
        context: Optional[dict[str, Any]] = None
        if instruction.context_ref is not None:
            context = self.cache.read_context_uri(instruction.context_ref)
        payload = {
            "decision_id": instruction.decision_id,
            "cloud_task_id": instruction.cloud_task_id,
            "dispatch_id": instruction.dispatch_id,
            "device_id": instruction.device_id,
            "sender_id": instruction.sender_id,
            "task_id": instruction.task_id,
            "bearing_id": instruction.bearing_id,
            "packet_id": instruction.packet_id,
            "attempt": instruction.attempt,
            "raw_packet": raw_packet,
            "context": context,
        }
        return JsonHttpClient(
            base_url, timeout_seconds=self.timeout_seconds
        ).post(instruction.endpoint, payload)
