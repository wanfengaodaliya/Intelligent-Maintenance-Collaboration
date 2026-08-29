# -*- coding: utf-8 -*-
"""严格校验、上下文队列和原始缓存的最小公共契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, TypeAlias


REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
EMPTY_CHANNEL_VALUES = "EMPTY_CHANNEL_VALUES"
INVALID_CHANNEL_UNIT = "INVALID_CHANNEL_UNIT"
INVALID_SAMPLE_RATE = "INVALID_SAMPLE_RATE"
INVALID_SAMPLE_COUNT = "INVALID_SAMPLE_COUNT"
SAMPLE_COUNT_MISMATCH = "SAMPLE_COUNT_MISMATCH"
NON_FINITE_VALUE = "NON_FINITE_VALUE"
VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
RAW_CACHE_WRITE_FAILED = "RAW_CACHE_WRITE_FAILED"

CACHE_PENDING = "PENDING"
CACHE_AVAILABLE = "AVAILABLE"
CACHE_VALIDATION_REJECTED = "VALIDATION_REJECTED"
CACHE_WRITE_FAILED = "CACHE_WRITE_FAILED"

CONTEXT_COMPLETE = "COMPLETE"
CONTEXT_PENDING = "PENDING_CONTEXT"
CONTEXT_INSUFFICIENT = "INSUFFICIENT_CONTEXT"
ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
ANCHOR_NOT_UNIQUE = "ANCHOR_NOT_UNIQUE"
INVALID_CONTEXT_REQUEST = "INVALID_CONTEXT_REQUEST"

RawPacketRef: TypeAlias = tuple[str, str, int]


@dataclass(frozen=True)
class ModuleStatus:
    success: bool
    error_code: Optional[str]


@dataclass(frozen=True)
class ModuleResult:
    status: ModuleStatus
    payload: Optional[dict[str, Any]]

    @classmethod
    def succeeded(cls, payload: dict[str, Any]) -> "ModuleResult":
        return cls(ModuleStatus(True, None), payload)

    @classmethod
    def failed(cls, error_code: str) -> "ModuleResult":
        return cls(ModuleStatus(False, error_code), None)


@dataclass(frozen=True)
class ValidationCacheInvocationContext:
    """由原始数据接入部分生成；时间不能在本模块内重新采集。"""

    received_at_ns: int


@dataclass(frozen=True)
class ContextSlotSnapshot:
    device_id: str
    bearing_id: str
    sender_id: str
    task_id: str
    packet_id: str
    sequence_number: int
    end_generate_timestamp_ns: Optional[int]
    received_at_ns: int
    cache_status: str
    raw_packet_ref: Optional[RawPacketRef]
    run_id: Optional[str] = None
