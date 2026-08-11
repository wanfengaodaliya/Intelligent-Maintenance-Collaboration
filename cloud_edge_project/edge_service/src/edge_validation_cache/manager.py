# -*- coding: utf-8 -*-
"""严格校验、原始包原子缓存、上下文查询和定时淘汰。"""
from __future__ import annotations

import math
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
from collections import deque
from dataclasses import dataclass
from numbers import Real
from typing import Any, Callable, Mapping, Optional

import numpy as np

from .config import ValidationCacheConfig
from .contracts import (
    ANCHOR_NOT_FOUND,
    ANCHOR_NOT_UNIQUE,
    CACHE_AVAILABLE,
    CACHE_PENDING,
    CACHE_VALIDATION_REJECTED,
    CACHE_WRITE_FAILED,
    CONTEXT_COMPLETE,
    CONTEXT_INSUFFICIENT,
    CONTEXT_PENDING,
    EMPTY_CHANNEL_VALUES,
    INVALID_CHANNEL_UNIT,
    INVALID_CONTEXT_REQUEST,
    INVALID_FIELD_TYPE,
    INVALID_SAMPLE_COUNT,
    INVALID_SAMPLE_RATE,
    INVALID_TIMESTAMP,
    NON_FINITE_VALUE,
    RAW_CACHE_WRITE_FAILED,
    REQUIRED_FIELD_MISSING,
    SAMPLE_COUNT_MISMATCH,
    VALUE_OUT_OF_RANGE,
    ContextSlotSnapshot,
    ModuleResult,
    RawPacketRef,
    ValidationCacheInvocationContext,
)


_IDENTITY_FIELDS = ("device_id", "bearing_id", "task_id", "packet_id", "sender_id")
_SEQUENCE_CHANNELS = (
    ("vibration", 64000, 3200, "mm/s"),
    ("phase_current_1_A", 64000, 3200, "A"),
    ("phase_current_2_A", 64000, 3200, "A"),
    ("shaft_speed_rpm", 4000, 200, None),
    ("load_torque_nm", 4000, 200, None),
    ("bearing_radial_load_n", 4000, 200, None),
)
_TEMPERATURE = "bearing_module_temperature_c"


class _ValidationError(ValueError):
    def __init__(self, error_code: str, scope: str, actual: object):
        super().__init__(scope)
        self.error_code = error_code
        self.scope = scope
        self.actual = actual


class _CacheError(RuntimeError):
    def __init__(self, scope: str, actual: object):
        super().__init__(scope)
        self.scope = scope
        self.actual = actual


@dataclass
class _ContextSlot:
    device_id: str
    bearing_id: str
    sender_id: str
    task_id: str
    packet_id: str
    sequence_number: int
    end_generate_timestamp_ns: Optional[int]
    received_at_ns: int
    cache_status: str
    raw_packet_ref: Optional[RawPacketRef] = None

    def snapshot(self) -> ContextSlotSnapshot:
        return ContextSlotSnapshot(**self.__dict__)


@dataclass
class _SenderCacheState:
    device_id: str
    bearing_id: str
    slots: deque[_ContextSlot]
    raw_packets: dict[RawPacketRef, dict[str, Any]]
    mutex: threading.Lock


class EdgeValidationCache:
    """接收已匹配唯一包；同步完成校验和原始缓存提交。"""

    def __init__(
        self,
        config: ValidationCacheConfig,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        on_error: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        errors = config.validate()
        if errors:
            raise ValueError("校验和缓存配置无效: " + "; ".join(errors))
        self.config = config
        self._clock_ns = clock_ns
        self._on_error = on_error or (lambda _: None)
        self._states: dict[str, _SenderCacheState] = {}
        self._states_mutex = threading.Lock()
        self._stop_event = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._retention_ns = int(config.raw_cache_retention_seconds * 1_000_000_000)
        self._pinned_refs: dict[RawPacketRef, int] = {}
        self._pins_mutex = threading.Lock()

    def process(
        self,
        raw_packet: Mapping[str, Any],
        context: ValidationCacheInvocationContext,
        raw_packet_ref: RawPacketRef,
    ) -> ModuleResult:
        """校验并缓存一个已匹配包；成功payload是供后续计算使用的独立副本。"""
        try:
            self._validate_context(context)
            identity = self._read_identity(raw_packet)
        except _ValidationError as exc:
            self._emit_error(exc.error_code, raw_packet, context, exc.scope, exc.actual)
            return ModuleResult.failed(exc.error_code)

        timestamp = raw_packet.get("end_generate_timestamp_ns")
        timestamp_valid = _positive_integer(timestamp)
        try:
            state, slot = self._append_slot(
                identity,
                int(timestamp) if timestamp_valid else None,
                context.received_at_ns,
                CACHE_PENDING if timestamp_valid else CACHE_VALIDATION_REJECTED,
            )
        except _CacheError as exc:
            self._emit_error(
                RAW_CACHE_WRITE_FAILED, raw_packet, context, exc.scope, exc.actual
            )
            return ModuleResult.failed(RAW_CACHE_WRITE_FAILED)

        if not timestamp_valid:
            self._emit_error(
                INVALID_TIMESTAMP,
                raw_packet,
                context,
                "end_generate_timestamp_ns",
                _actual(timestamp),
            )
            return ModuleResult.failed(INVALID_TIMESTAMP)

        try:
            validated_packet = self._validate_data(raw_packet, identity, int(timestamp))
        except _ValidationError as exc:
            self._set_slot_failure(state, slot, CACHE_VALIDATION_REJECTED)
            self._emit_error(exc.error_code, raw_packet, context, exc.scope, exc.actual)
            return ModuleResult.failed(exc.error_code)

        try:
            self._store(state, slot, raw_packet_ref, validated_packet)
        except _CacheError as exc:
            self._set_slot_failure(state, slot, CACHE_WRITE_FAILED)
            self._emit_error(
                RAW_CACHE_WRITE_FAILED, raw_packet, context, exc.scope, exc.actual
            )
            return ModuleResult.failed(RAW_CACHE_WRITE_FAILED)
        return ModuleResult.succeeded(validated_packet)

    def read(self, raw_packet_ref: RawPacketRef) -> Optional[dict[str, Any]]:
        """按稳定逻辑键读取原始包，返回与缓存不共享数组的只读副本。"""
        if not _valid_raw_packet_ref(raw_packet_ref):
            return None
        state = self._existing_state(raw_packet_ref[0])
        if state is None:
            return None
        now_ns = self._read_clock()
        with state.mutex:
            self._cleanup_state_locked(state, now_ns)
            packet = state.raw_packets.get(raw_packet_ref)
            return _clone_packet(packet, readonly=True) if packet is not None else None

    @staticmethod
    def raw_data_uri(raw_packet_ref: RawPacketRef) -> str:
        if not _valid_raw_packet_ref(raw_packet_ref):
            raise ValueError("raw_packet_ref 非法")
        sender_id, task_id, sequence_number = raw_packet_ref
        return "edge-cache://%s/%s/%d" % (
            quote(sender_id, safe=""),
            quote(task_id, safe=""),
            sequence_number,
        )

    @staticmethod
    def raw_ref_from_uri(uri: str) -> RawPacketRef:
        parsed = urlparse(uri)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme != "edge-cache" or not parsed.netloc or len(parts) != 2:
            raise ValueError("raw_data_ref 必须是 edge-cache://sender/task/sequence")
        try:
            result: RawPacketRef = (
                unquote(parsed.netloc),
                unquote(parts[0]),
                int(parts[1]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("raw_data_ref 非法") from exc
        if not _valid_raw_packet_ref(result):
            raise ValueError("raw_data_ref 非法")
        return result

    def read_uri(self, uri: str) -> Optional[dict[str, Any]]:
        return self.read(self.raw_ref_from_uri(uri))

    def pin(self, raw_packet_ref: RawPacketRef) -> bool:
        """锁定仍待云端处理的数据，避免按保留期清理。"""
        if self.read(raw_packet_ref) is None:
            return False
        with self._pins_mutex:
            self._pinned_refs[raw_packet_ref] = self._pinned_refs.get(raw_packet_ref, 0) + 1
        return True

    def unpin(self, raw_packet_ref: RawPacketRef) -> bool:
        with self._pins_mutex:
            count = self._pinned_refs.get(raw_packet_ref)
            if count is None:
                return False
            if count <= 1:
                self._pinned_refs.pop(raw_packet_ref, None)
            else:
                self._pinned_refs[raw_packet_ref] = count - 1
        return True

    def pin_many(self, raw_packet_refs: tuple[RawPacketRef, ...]) -> bool:
        """Pin a complete fixed window, rolling back when any reference is absent."""
        pinned: list[RawPacketRef] = []
        for raw_packet_ref in raw_packet_refs:
            if self.pin(raw_packet_ref):
                pinned.append(raw_packet_ref)
                continue
            for existing in pinned:
                self.unpin(existing)
            return False
        return True

    def unpin_many(self, raw_packet_refs: tuple[RawPacketRef, ...]) -> None:
        for raw_packet_ref in raw_packet_refs:
            self.unpin(raw_packet_ref)

    def pin_uri(self, uri: str) -> bool:
        return self.pin(self.raw_ref_from_uri(uri))

    def unpin_uri(self, uri: str) -> bool:
        return self.unpin(self.raw_ref_from_uri(uri))

    @staticmethod
    def context_uri(
        *,
        device_id: str,
        bearing_id: str,
        sender_id: str,
        anchor_packet_id: str,
        anchor_end_generate_timestamp_ns: int,
    ) -> str:
        values = (device_id, bearing_id, sender_id, anchor_packet_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("context_ref 身份字段必须是非空字符串")
        if not _positive_integer(anchor_end_generate_timestamp_ns):
            raise ValueError("anchor_end_generate_timestamp_ns 必须是正整数")
        return "edge-context://%s/%s/%s/%s?end_ns=%d" % (
            *(quote(value, safe="") for value in values),
            anchor_end_generate_timestamp_ns,
        )

    def read_context_uri(self, uri: str, *, requested_at_ns: Optional[int] = None) -> dict[str, Any]:
        parsed = urlparse(uri)
        parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        if parsed.scheme != "edge-context" or not parsed.netloc or len(parts) != 3:
            raise ValueError("context_ref 非法")
        try:
            end_ns = int(query["end_ns"][0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("context_ref 缺少有效 end_ns") from exc
        request = {
            "request_id": "context-upload-%d" % self._read_clock(),
            "device_id": unquote(parsed.netloc),
            "bearing_id": unquote(parts[0]),
            "sender_id": unquote(parts[1]),
            "anchor_packet_id": unquote(parts[2]),
            "anchor_end_generate_timestamp_ns": end_ns,
            "requested_at_ns": requested_at_ns or self._read_clock(),
            "before_packet_count": self.config.context_before_packet_count,
        }
        return self.query_context(request)

    def context_snapshot(self, sender_id: str) -> tuple[ContextSlotSnapshot, ...]:
        """返回上下文槽位的不可变快照，供编排状态和测试核对。"""
        state = self._existing_state(sender_id)
        if state is None:
            return ()
        with state.mutex:
            return tuple(slot.snapshot() for slot in state.slots)

    def query_context(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """查询锚点前固定20个接收位置，不跨发送器或轴承补位。"""
        if not self._valid_context_request(request):
            return self._query_result(request, INVALID_CONTEXT_REQUEST)

        sender_id = request["sender_id"]
        state = self._existing_state(sender_id)
        if state is None:
            return self._query_result(request, ANCHOR_NOT_FOUND)

        now_ns = self._read_clock()
        with state.mutex:
            self._cleanup_state_locked(state, now_ns)
            if (
                state.device_id != request["device_id"]
                or state.bearing_id != request["bearing_id"]
            ):
                return self._query_result(request, INVALID_CONTEXT_REQUEST)

            slots = list(state.slots)
            matches = [
                index
                for index, slot in enumerate(slots)
                if slot.packet_id == request["anchor_packet_id"]
                and slot.end_generate_timestamp_ns
                == request["anchor_end_generate_timestamp_ns"]
                and slot.device_id == request["device_id"]
                and slot.bearing_id == request["bearing_id"]
            ]
            if not matches:
                return self._query_result(request, ANCHOR_NOT_FOUND)
            if len(matches) != 1:
                return self._query_result(request, ANCHOR_NOT_UNIQUE)

            expected = self.config.context_before_packet_count
            anchor_index = matches[0]
            start_index = max(0, anchor_index - expected)
            selected = slots[start_index:anchor_index]
            shortage = expected - len(selected)
            unavailable: list[dict[str, Any]] = [
                {
                    "position_from_anchor": position,
                    "packet_id": None,
                    "sequence_number": None,
                    "cache_status": "NOT_AVAILABLE",
                }
                for position in range(-expected, -expected + shortage)
            ]
            packets: list[dict[str, Any]] = []
            has_permanent_gap = shortage > 0
            has_pending = False

            for slot in selected:
                if (
                    slot.device_id != request["device_id"]
                    or slot.bearing_id != request["bearing_id"]
                ):
                    has_permanent_gap = True
                    unavailable.append(_unavailable_slot(slot, "IDENTITY_MISMATCH"))
                elif slot.cache_status == CACHE_PENDING:
                    has_pending = True
                    unavailable.append(_unavailable_slot(slot, CACHE_PENDING))
                elif slot.cache_status == CACHE_AVAILABLE and slot.raw_packet_ref is not None:
                    packet = state.raw_packets.get(slot.raw_packet_ref)
                    if packet is None:
                        has_permanent_gap = True
                        unavailable.append(_unavailable_slot(slot, "MISSING_CACHE_ENTRY"))
                    else:
                        packets.append(_clone_packet(packet, readonly=True))
                else:
                    has_permanent_gap = True
                    unavailable.append(_unavailable_slot(slot, slot.cache_status))

            if has_permanent_gap:
                status = CONTEXT_INSUFFICIENT
            elif has_pending:
                status = CONTEXT_PENDING
            else:
                status = CONTEXT_COMPLETE
            return self._query_result(
                request,
                status,
                packets=packets,
                unavailable_packets=unavailable,
            )

    def cleanup_expired(self, now_ns: Optional[int] = None) -> int:
        """删除所有发送器中达到保留时间的槽位和对应完整包。"""
        current_ns = self._read_clock() if now_ns is None else now_ns
        if not _positive_integer(current_ns):
            raise ValueError("now_ns 必须是正整数")
        with self._states_mutex:
            states = list(self._states.values())
        removed = 0
        for state in states:
            with state.mutex:
                removed += self._cleanup_state_locked(state, int(current_ns))
        return removed

    def start(self) -> None:
        """启动后台定期淘汰；重复调用不会创建多个线程。"""
        with self._states_mutex:
            if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
                return
            self._stop_event.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="edge-raw-cache-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._cleanup_thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.config.cache_cleanup_interval_seconds + 0.5))

    def _append_slot(
        self,
        identity: dict[str, Any],
        timestamp: Optional[int],
        received_at_ns: int,
        status: str,
    ) -> tuple[_SenderCacheState, _ContextSlot]:
        state = self._state_for(identity)
        with state.mutex:
            self._cleanup_state_locked(state, received_at_ns)
            if state.slots:
                if (
                    state.device_id != identity["device_id"]
                    or state.bearing_id != identity["bearing_id"]
                ):
                    raise _CacheError(
                        "sender_binding",
                        {
                            "bound_device_id": state.device_id,
                            "bound_bearing_id": state.bearing_id,
                        },
                    )
            else:
                state.device_id = identity["device_id"]
                state.bearing_id = identity["bearing_id"]
            if len(state.slots) >= self.config.context_queue_capacity_per_sender:
                raise _CacheError("context_queue_capacity", len(state.slots))
            slot = _ContextSlot(
                **identity,
                end_generate_timestamp_ns=timestamp,
                received_at_ns=received_at_ns,
                cache_status=status,
            )
            state.slots.append(slot)
            return state, slot

    def _store(
        self,
        state: _SenderCacheState,
        slot: _ContextSlot,
        raw_packet_ref: RawPacketRef,
        validated_packet: dict[str, Any],
    ) -> None:
        expected_ref = (
            validated_packet["sender_id"],
            validated_packet["task_id"],
            validated_packet["sequence_number"],
        )
        if not _valid_raw_packet_ref(raw_packet_ref) or raw_packet_ref != expected_ref:
            raise _CacheError("raw_packet_ref", _actual(raw_packet_ref))
        cache_packet = _clone_packet(validated_packet, readonly=True)
        with state.mutex:
            if slot.cache_status != CACHE_PENDING or slot.raw_packet_ref is not None:
                raise _CacheError("context_slot_state", slot.cache_status)
            if raw_packet_ref in state.raw_packets:
                raise _CacheError("raw_packet_ref_conflict", raw_packet_ref)
            if len(state.raw_packets) >= self.config.raw_cache_capacity_per_sender:
                raise _CacheError("raw_cache_capacity", len(state.raw_packets))
            state.raw_packets[raw_packet_ref] = cache_packet
            slot.raw_packet_ref = raw_packet_ref
            slot.cache_status = CACHE_AVAILABLE

    @staticmethod
    def _set_slot_failure(
        state: _SenderCacheState, slot: _ContextSlot, status: str
    ) -> None:
        with state.mutex:
            if any(existing is slot for existing in state.slots) and slot.cache_status == CACHE_PENDING:
                slot.raw_packet_ref = None
                slot.cache_status = status

    def _state_for(self, identity: Mapping[str, Any]) -> _SenderCacheState:
        sender_id = identity["sender_id"]
        with self._states_mutex:
            state = self._states.get(sender_id)
            if state is None:
                state = _SenderCacheState(
                    device_id=identity["device_id"],
                    bearing_id=identity["bearing_id"],
                    slots=deque(),
                    raw_packets={},
                    mutex=threading.Lock(),
                )
                self._states[sender_id] = state
            return state

    def _existing_state(self, sender_id: str) -> Optional[_SenderCacheState]:
        with self._states_mutex:
            return self._states.get(sender_id)

    def _cleanup_state_locked(self, state: _SenderCacheState, now_ns: int) -> int:
        removed = 0
        retained: deque[_ContextSlot] = deque()
        with self._pins_mutex:
            pinned = set(self._pinned_refs)
        for slot in state.slots:
            expired = now_ns - slot.received_at_ns >= self._retention_ns
            if expired and slot.raw_packet_ref not in pinned:
                if slot.raw_packet_ref is not None:
                    state.raw_packets.pop(slot.raw_packet_ref, None)
                removed += 1
            else:
                retained.append(slot)
        state.slots = retained
        return removed

    @staticmethod
    def _validate_context(context: ValidationCacheInvocationContext) -> None:
        received = getattr(context, "received_at_ns", None)
        if not _positive_integer(received):
            raise _ValidationError(
                INVALID_FIELD_TYPE, "received_at_ns", _actual(received)
            )

    @staticmethod
    def _read_identity(packet: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, Mapping):
            raise _ValidationError(INVALID_FIELD_TYPE, "raw_packet", type(packet).__name__)
        identity: dict[str, Any] = {}
        for field in _IDENTITY_FIELDS:
            if field not in packet:
                raise _ValidationError(REQUIRED_FIELD_MISSING, field, "missing")
            value = packet[field]
            if not isinstance(value, str) or not value.strip():
                raise _ValidationError(INVALID_FIELD_TYPE, field, _actual(value))
            identity[field] = value
        sequence = packet.get("sequence_number")
        if "sequence_number" not in packet:
            raise _ValidationError(REQUIRED_FIELD_MISSING, "sequence_number", "missing")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise _ValidationError(
                INVALID_FIELD_TYPE, "sequence_number", _actual(sequence)
            )
        identity["sequence_number"] = sequence
        return identity

    def _validate_data(
        self,
        packet: Mapping[str, Any],
        identity: Mapping[str, Any],
        timestamp: int,
    ) -> dict[str, Any]:
        if "data" not in packet:
            raise _ValidationError(REQUIRED_FIELD_MISSING, "data", "missing")
        data = packet["data"]
        if not isinstance(data, Mapping):
            raise _ValidationError(INVALID_FIELD_TYPE, "data", _actual(data))

        for channel, _, _, _ in _SEQUENCE_CHANNELS:
            if channel not in data:
                raise _ValidationError(REQUIRED_FIELD_MISSING, channel, "missing")
        if _TEMPERATURE not in data:
            raise _ValidationError(REQUIRED_FIELD_MISSING, _TEMPERATURE, "missing")

        prepared: dict[str, tuple[Mapping[str, Any], np.ndarray]] = {}
        for channel, _, _, unit in _SEQUENCE_CHANNELS:
            source = data[channel]
            if not isinstance(source, Mapping):
                raise _ValidationError(INVALID_FIELD_TYPE, channel, _actual(source))
            required = ["sample_rate_hz", "sample_count", "values"]
            if unit is not None:
                required.insert(0, "unit")
            for field in required:
                if field not in source:
                    raise _ValidationError(
                        REQUIRED_FIELD_MISSING, f"{channel}.{field}", "missing"
                    )
            if isinstance(source["sample_rate_hz"], bool) or not isinstance(
                source["sample_rate_hz"], int
            ):
                raise _ValidationError(
                    INVALID_FIELD_TYPE,
                    f"{channel}.sample_rate_hz",
                    _actual(source["sample_rate_hz"]),
                )
            if isinstance(source["sample_count"], bool) or not isinstance(
                source["sample_count"], int
            ):
                raise _ValidationError(
                    INVALID_FIELD_TYPE,
                    f"{channel}.sample_count",
                    _actual(source["sample_count"]),
                )
            if unit is not None and not isinstance(source["unit"], str):
                raise _ValidationError(
                    INVALID_FIELD_TYPE, f"{channel}.unit", _actual(source["unit"])
                )
            values = source["values"]
            if not isinstance(values, (list, tuple, np.ndarray)):
                raise _ValidationError(
                    INVALID_FIELD_TYPE, f"{channel}.values", _actual(values)
                )
            array_object = np.asarray(values, dtype=object)
            if array_object.ndim != 1:
                raise _ValidationError(
                    INVALID_FIELD_TYPE,
                    f"{channel}.values",
                    f"ndim={array_object.ndim}",
                )
            if any(not _numeric_value(value) for value in array_object):
                raise _ValidationError(
                    INVALID_FIELD_TYPE, f"{channel}.values", "non-numeric element"
                )
            prepared[channel] = (source, np.asarray(values, dtype=np.float64))

        temperature = data[_TEMPERATURE]
        if not _numeric_value(temperature):
            raise _ValidationError(
                INVALID_FIELD_TYPE, _TEMPERATURE, _actual(temperature)
            )

        for channel, _, _, unit in _SEQUENCE_CHANNELS:
            if unit is not None and prepared[channel][0]["unit"] != unit:
                raise _ValidationError(
                    INVALID_CHANNEL_UNIT,
                    f"{channel}.unit",
                    prepared[channel][0]["unit"],
                )
        for channel, sample_rate, _, _ in _SEQUENCE_CHANNELS:
            actual_rate = prepared[channel][0]["sample_rate_hz"]
            if actual_rate != sample_rate:
                raise _ValidationError(
                    INVALID_SAMPLE_RATE, f"{channel}.sample_rate_hz", actual_rate
                )
        for channel, _, sample_count, _ in _SEQUENCE_CHANNELS:
            actual_count = prepared[channel][0]["sample_count"]
            if actual_count != sample_count:
                raise _ValidationError(
                    INVALID_SAMPLE_COUNT, f"{channel}.sample_count", actual_count
                )
        for channel, _, _, _ in _SEQUENCE_CHANNELS:
            if prepared[channel][1].size == 0:
                raise _ValidationError(
                    EMPTY_CHANNEL_VALUES, f"{channel}.values", "empty"
                )
        for channel, _, _, _ in _SEQUENCE_CHANNELS:
            declared = prepared[channel][0]["sample_count"]
            actual_length = prepared[channel][1].size
            if actual_length != declared:
                raise _ValidationError(
                    SAMPLE_COUNT_MISMATCH,
                    f"{channel}.values",
                    actual_length,
                )
        for channel, _, _, _ in _SEQUENCE_CHANNELS:
            if not np.isfinite(prepared[channel][1]).all():
                raise _ValidationError(
                    NON_FINITE_VALUE, f"{channel}.values", "non-finite element"
                )
        if not math.isfinite(float(temperature)):
            raise _ValidationError(NON_FINITE_VALUE, _TEMPERATURE, _actual(temperature))

        for channel, value_range in self.config.hard_value_ranges.items():
            if channel == _TEMPERATURE:
                values_to_check = np.asarray([temperature], dtype=np.float64)
            else:
                values_to_check = prepared[channel][1]
            if (
                np.any(values_to_check < float(value_range.minimum))
                or np.any(values_to_check > float(value_range.maximum))
            ):
                raise _ValidationError(
                    VALUE_OUT_OF_RANGE,
                    channel,
                    {
                        "minimum": float(np.min(values_to_check)),
                        "maximum": float(np.max(values_to_check)),
                    },
                )

        validated_data: dict[str, Any] = {}
        for channel, sample_rate, sample_count, unit in _SEQUENCE_CHANNELS:
            source, values = prepared[channel]
            channel_result: dict[str, Any] = {
                "sample_rate_hz": sample_rate,
                "sample_count": sample_count,
                "values": values.copy(),
            }
            if unit is not None:
                channel_result["unit"] = source["unit"]
            validated_data[channel] = channel_result
        validated_data[_TEMPERATURE] = float(temperature)
        return {
            **identity,
            "end_generate_timestamp_ns": timestamp,
            "data": validated_data,
        }

    def _valid_context_request(self, request: Mapping[str, Any]) -> bool:
        if not isinstance(request, Mapping):
            return False
        for field in (
            "request_id",
            "device_id",
            "bearing_id",
            "sender_id",
            "anchor_packet_id",
        ):
            value = request.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
        for field in ("anchor_end_generate_timestamp_ns", "requested_at_ns"):
            if not _positive_integer(request.get(field)):
                return False
        before = request.get("before_packet_count")
        return (
            not isinstance(before, bool)
            and isinstance(before, int)
            and before == self.config.context_before_packet_count
        )

    def _query_result(
        self,
        request: Mapping[str, Any],
        status: str,
        *,
        packets: Optional[list[dict[str, Any]]] = None,
        unavailable_packets: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        source = request if isinstance(request, Mapping) else {}
        result = {
            "request_id": source.get("request_id"),
            "device_id": source.get("device_id"),
            "bearing_id": source.get("bearing_id"),
            "sender_id": source.get("sender_id"),
            "anchor_packet_id": source.get("anchor_packet_id"),
            "anchor_end_generate_timestamp_ns": source.get(
                "anchor_end_generate_timestamp_ns"
            ),
            "expected_count": self.config.context_before_packet_count,
            "available_count": len(packets or []),
            "context_status": status,
            "unavailable_packets": unavailable_packets or [],
            "packets": packets or [],
        }
        return result

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self.config.cache_cleanup_interval_seconds):
            try:
                self.cleanup_expired()
            except Exception as exc:
                self._emit_error(
                    RAW_CACHE_WRITE_FAILED,
                    {},
                    ValidationCacheInvocationContext(0),
                    "background_cleanup",
                    type(exc).__name__,
                )

    def _read_clock(self) -> int:
        value = self._clock_ns()
        if not _positive_integer(value):
            raise ValueError("clock_ns 必须返回正整数")
        return int(value)

    def _emit_error(
        self,
        error_code: str,
        packet: Mapping[str, Any],
        context: ValidationCacheInvocationContext,
        scope: str,
        actual: object,
    ) -> None:
        source = packet if isinstance(packet, Mapping) else {}
        log = {field: source.get(field) for field in _IDENTITY_FIELDS}
        log.update(
            {
                "sequence_number": source.get("sequence_number"),
                "received_at_ns": getattr(context, "received_at_ns", None),
                "stage": "validation_cache",
                "scope": scope,
                "error_code": error_code,
                "actual": _actual(actual),
                "action": "current_packet_stopped",
            }
        )
        try:
            self._on_error(log)
        except Exception:
            pass


def _clone_packet(packet: Mapping[str, Any], *, readonly: bool) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for channel, _, _, unit in _SEQUENCE_CHANNELS:
        source = packet["data"][channel]
        values = np.asarray(source["values"], dtype=np.float64).copy()
        if readonly:
            values.setflags(write=False)
        channel_result: dict[str, Any] = {
            "sample_rate_hz": source["sample_rate_hz"],
            "sample_count": source["sample_count"],
            "values": values,
        }
        if unit is not None:
            channel_result["unit"] = source["unit"]
        data[channel] = channel_result
    data[_TEMPERATURE] = float(packet["data"][_TEMPERATURE])
    return {
        **{field: packet[field] for field in _IDENTITY_FIELDS},
        "sequence_number": packet["sequence_number"],
        "end_generate_timestamp_ns": packet["end_generate_timestamp_ns"],
        "data": data,
    }


def _unavailable_slot(slot: _ContextSlot, status: str) -> dict[str, Any]:
    return {
        "position_from_anchor": None,
        "packet_id": slot.packet_id,
        "sequence_number": slot.sequence_number,
        "cache_status": status,
    }


def _positive_integer(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _numeric_value(value: object) -> bool:
    return not isinstance(value, (bool, np.bool_)) and isinstance(value, Real)


def _valid_raw_packet_ref(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[0], str)
        and bool(value[0].strip())
        and isinstance(value[1], str)
        and bool(value[1].strip())
        and not isinstance(value[2], bool)
        and isinstance(value[2], int)
    )


def _actual(value: object) -> object:
    if isinstance(value, np.ndarray):
        return {"type": "ndarray", "shape": value.shape}
    if isinstance(value, Mapping):
        return {"type": type(value).__name__, "keys": sorted(map(str, value.keys()))}
    if isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "length": len(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__
