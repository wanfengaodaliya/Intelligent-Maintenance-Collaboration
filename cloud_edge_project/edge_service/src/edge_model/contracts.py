# -*- coding: utf-8 -*-
"""边缘模型运行契约。

对外：
    EdgeResult          扁平 4 字段
    PacketResult        当前包身份 + 该包的 EdgeResult

内部：
    PacketInferenceTask 一个 PerceptionResult 对应的独立推理任务
    RunRecord           一次包级诊断的执行记录
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


EDGE_RESULT_VALUES = ("normal", "warning", "fault")
EDGE_RISK_VALUES = ("low", "medium", "high")

EXECUTION_LOCAL_MODEL = "LOCAL_MODEL"
EXECUTION_CODE_FALLBACK = "CODE_FALLBACK"

REASON_QUEUE_FULL = "QUEUE_FULL"
REASON_QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
REASON_TOTAL_TIMEOUT = "TOTAL_TIMEOUT"
REASON_MODEL_BUSY = "MODEL_BUSY"
REASON_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
REASON_MODEL_INFERENCE_FAILED = "MODEL_INFERENCE_FAILED"
REASON_MODEL_INFERENCE_TIMEOUT = "MODEL_INFERENCE_TIMEOUT"
REASON_MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
REASON_MODEL_INPUT_INVALID = "MODEL_INPUT_INVALID"
REASON_BREAKER_OPEN = "BREAKER_OPEN"
REASON_CODE_FALLBACK_FAILED = "CODE_FALLBACK_FAILED"

HTTP_ERROR_MODEL_BUSY = "MODEL_BUSY"
HTTP_ERROR_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
HTTP_ERROR_MODEL_INFERENCE_FAILED = "MODEL_INFERENCE_FAILED"
HTTP_ERROR_MODEL_INFERENCE_TIMEOUT = "MODEL_INFERENCE_TIMEOUT"
HTTP_ERROR_MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
HTTP_ERROR_MODEL_INPUT_INVALID = "MODEL_INPUT_INVALID"


@dataclass(frozen=True)
class EdgeResult:
    """对外统一结果（扁平 4 字段，接口冻结）。"""

    edge_result: str
    confidence: float
    edge_risk_level: str
    model_version: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "edge_result": self.edge_result,
            "confidence": self.confidence,
            "edge_risk_level": self.edge_risk_level,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class PacketResult:
    """每包输出：包身份 + 当前包独立产生的 EdgeResult。"""

    device_id: str
    bearing_id: str
    task_id: str
    packet_id: str
    sender_id: str
    sequence_number: int
    edge: EdgeResult

    def as_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "bearing_id": self.bearing_id,
            "task_id": self.task_id,
            "packet_id": self.packet_id,
            "sender_id": self.sender_id,
            "sequence_number": self.sequence_number,
            **self.edge.as_dict(),
        }


@dataclass
class PacketInferenceTask:
    """一个数据包的内部模型任务；不含任何跨包聚合数据。"""

    request_id: str
    device_id: str
    bearing_id: str
    task_id: str
    packet_id: str
    sender_id: str
    sequence_number: int
    perception: Dict[str, Any]
    submit_ts: Optional[float] = None
    started_at_ns: Optional[int] = None


@dataclass(frozen=True)
class PacketExecutionCompleted:
    """供边缘运行编排使用的每包唯一终态事件。"""

    request_id: str
    device_id: str
    bearing_id: str
    task_id: str
    packet_id: str
    sender_id: str
    sequence_number: int
    status: str
    error_code: Optional[str]
    started_at_ns: int
    finished_at_ns: int
    edge: Optional[EdgeResult]


@dataclass
class RunRecord:
    """一次包级诊断的内部执行记录。"""

    request_id: str
    device_id: str
    bearing_id: str
    task_id: str
    packet_id: str
    sender_id: str
    sequence_number: int
    execution_mode: str
    fallback_reason: Optional[str]
    output_valid: bool = True
    edge_result: Optional[str] = None
    edge_risk_level: Optional[str] = None
    confidence: Optional[float] = None
    model_version: Optional[str] = None
    queue_wait_ms: Optional[float] = None
    inference_latency_ms: Optional[float] = None
    total_latency_ms: Optional[float] = None
    exceeded_total_timeout: bool = False
    breaker_state: Optional[str] = None
    note: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "device_id": self.device_id,
            "bearing_id": self.bearing_id,
            "task_id": self.task_id,
            "packet_id": self.packet_id,
            "sender_id": self.sender_id,
            "sequence_number": self.sequence_number,
            "output_valid": self.output_valid,
            "execution_mode": self.execution_mode,
            "fallback_reason": self.fallback_reason,
            "edge_result": self.edge_result,
            "edge_risk_level": self.edge_risk_level,
            "confidence": self.confidence,
            "model_version": self.model_version,
            "queue_wait_ms": self.queue_wait_ms,
            "inference_latency_ms": self.inference_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "exceeded_total_timeout": self.exceeded_total_timeout,
            "breaker_state": self.breaker_state,
            "note": self.note,
        }
