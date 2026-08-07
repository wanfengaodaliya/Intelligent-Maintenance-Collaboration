# -*- coding: utf-8 -*-
"""任务接入、包匹配和处理记录的最小公共契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from edge_validation_cache import RawPacketRef


SUPPORTED_TASK_TYPE = "BEARING_EDGE_INFERENCE"
EXPECTED_PACKET_COUNT = 80

ACK_ACCEPTED = "ACCEPTED"
ACK_REJECTED = "REJECTED"

INVALID_TASK = "INVALID_TASK"
TARGET_NODE_MISMATCH = "TARGET_NODE_MISMATCH"
UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"
TASK_CONFLICT = "TASK_CONFLICT"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
TASK_NOT_ACCEPTING_INPUT = "TASK_NOT_ACCEPTING_INPUT"
INPUT_REFERENCE_MISMATCH = "INPUT_REFERENCE_MISMATCH"
INVALID_SEQUENCE_NUMBER = "INVALID_SEQUENCE_NUMBER"
TASK_SEQUENCE_CONFLICT = "TASK_SEQUENCE_CONFLICT"
PACKET_CONTENT_CONFLICT = "PACKET_CONTENT_CONFLICT"
PACKET_NOT_RECEIVED = "PACKET_NOT_RECEIVED"

INGRESS_ACCEPTED = "ACCEPTED_FOR_PROCESSING"
INGRESS_DUPLICATE = "DUPLICATE"
INGRESS_REJECTED = "REJECTED"

TASK_WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
TASK_RUNNING = "RUNNING"

PACKET_PROCESSING = "PROCESSING"
PACKET_VALIDATION_REJECTED = "VALIDATION_REJECTED"
PACKET_PROCESSING_FAILED = "PROCESSING_FAILED"
PACKET_NOT_RECEIVED_STATUS = "NOT_RECEIVED"

STAGE_VALIDATION = "VALIDATION"
STAGE_DOWNSAMPLING = "DOWNSAMPLING"

COMPLETENESS_PENDING = "PENDING"
COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class TaskAck:
    task_id: Optional[str]
    edge_node_id: str
    ack_status: str
    reason_code: Optional[str]
    received_at_ns: int
    acknowledged_at_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "edge_node_id": self.edge_node_id,
            "ack_status": self.ack_status,
            "reason_code": self.reason_code,
            "received_at_ns": self.received_at_ns,
            "acknowledged_at_ns": self.acknowledged_at_ns,
        }


@dataclass
class PacketRecord:
    device_id: str
    bearing_id: str
    task_id: str
    packet_id: Optional[str]
    sender_id: str
    sequence_number: int
    packet_status: str
    current_stage: Optional[str]
    received_at_ns: Optional[int]
    finished_at_ns: Optional[int] = None
    error_code: Optional[str] = None
    raw_packet_ref: Optional[RawPacketRef] = None
    content_fingerprint: Optional[str] = None
    edge_output: Optional[dict[str, Any]] = None
    summary_generated: bool = False


@dataclass
class BearingTaskRecord:
    task_id: str
    device_id: str
    bearing_id: str
    sender_id: str
    expected_packet_count: int = EXPECTED_PACKET_COUNT
    received_packet_count: int = 0
    received_sequence_numbers: set[int] = field(default_factory=set)
    terminal_packet_count: int = 0
    final_edge_count: int = 0
    final_cloud_count: int = 0
    validation_rejected_count: int = 0
    processing_failed_count: int = 0
    summary_generated_count: int = 0
    end_sequence_received: bool = False
    missing_sequence_numbers: tuple[int, ...] = ()
    missing_packet_count: int = 0
    data_completeness: str = COMPLETENESS_PENDING
    packet_records: dict[int, PacketRecord] = field(default_factory=dict)
    bearing_task_result: Optional[dict[str, Any]] = None


@dataclass
class TaskRecord:
    task_id: str
    target_edge_node_id: str
    task_type: str
    device_id: str
    expected_bearing_ids: tuple[str, ...]
    assigned_bearing_ids: tuple[str, ...]
    dispatched_at_ns: int
    task_status: str
    received_at_ns: int
    acknowledged_at_ns: int
    bearing_task_records: dict[str, BearingTaskRecord]
    started_at_ns: Optional[int] = None
    finished_at_ns: Optional[int] = None
    completed_bearing_count: int = 0
    device_result_status: str = "PENDING"
    output: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class PacketIngressResult:
    status: str
    error_code: Optional[str]
    received_at_ns: int
    packet_record: Optional[PacketRecord]
    validated_packet: Optional[dict[str, Any]]
