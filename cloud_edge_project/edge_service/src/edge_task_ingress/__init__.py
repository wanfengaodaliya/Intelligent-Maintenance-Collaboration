# -*- coding: utf-8 -*-
"""任务接入、注册表和原始数据匹配。"""

from .config import TaskIngressConfig
from .contracts import (
    EXPECTED_PACKET_COUNT,
    INGRESS_ACCEPTED,
    INGRESS_DUPLICATE,
    INGRESS_REJECTED,
    INPUT_REFERENCE_MISMATCH,
    INVALID_SEQUENCE_NUMBER,
    INVALID_TASK,
    PACKET_CONTENT_CONFLICT,
    PACKET_NOT_RECEIVED,
    SUPPORTED_TASK_TYPE,
    TARGET_NODE_MISMATCH,
    TASK_CONFLICT,
    TASK_NOT_FOUND,
    TASK_SEQUENCE_CONFLICT,
    UNSUPPORTED_TASK_TYPE,
    BearingTaskRecord,
    PacketIngressResult,
    PacketRecord,
    TaskAck,
    TaskRecord,
)
from .manager import EdgeTaskIngress

__all__ = [
    "BearingTaskRecord",
    "EXPECTED_PACKET_COUNT",
    "EdgeTaskIngress",
    "INGRESS_ACCEPTED",
    "INGRESS_DUPLICATE",
    "INGRESS_REJECTED",
    "INPUT_REFERENCE_MISMATCH",
    "INVALID_SEQUENCE_NUMBER",
    "INVALID_TASK",
    "PACKET_CONTENT_CONFLICT",
    "PACKET_NOT_RECEIVED",
    "PacketIngressResult",
    "PacketRecord",
    "SUPPORTED_TASK_TYPE",
    "TARGET_NODE_MISMATCH",
    "TASK_CONFLICT",
    "TASK_NOT_FOUND",
    "TASK_SEQUENCE_CONFLICT",
    "TaskAck",
    "TaskIngressConfig",
    "TaskRecord",
    "UNSUPPORTED_TASK_TYPE",
]
