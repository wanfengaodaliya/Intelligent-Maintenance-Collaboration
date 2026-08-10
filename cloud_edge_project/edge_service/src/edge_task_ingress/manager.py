# -*- coding: utf-8 -*-
"""任务原子注册、包匹配、去重冲突判断和校验缓存编排。"""
from __future__ import annotations

import copy
import hashlib
import math
import struct
import threading
import time
from dataclasses import dataclass
from numbers import Real
from typing import Any, Callable, Mapping, Optional

import numpy as np

from edge_validation_cache import EdgeValidationCache, ValidationCacheInvocationContext
from edge_validation_cache.contracts import RAW_CACHE_WRITE_FAILED

from .config import TaskIngressConfig
from .contracts import (
    ACK_ACCEPTED,
    ACK_REJECTED,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INCOMPLETE,
    EXPECTED_PACKET_COUNT,
    INGRESS_ACCEPTED,
    INGRESS_DUPLICATE,
    INGRESS_REJECTED,
    INPUT_REFERENCE_MISMATCH,
    INVALID_SEQUENCE_NUMBER,
    INVALID_TASK,
    PACKET_CONTENT_CONFLICT,
    PACKET_NOT_RECEIVED,
    PACKET_NOT_RECEIVED_STATUS,
    PACKET_PROCESSING,
    PACKET_PROCESSING_FAILED,
    PACKET_SUCCEEDED,
    PACKET_VALIDATION_REJECTED,
    STAGE_DOWNSAMPLING,
    STAGE_VALIDATION,
    SUPPORTED_TASK_TYPE,
    TARGET_NODE_MISMATCH,
    TASK_CONFLICT,
    TASK_NOT_ACCEPTING_INPUT,
    TASK_NOT_FOUND,
    TASK_REVOKED,
    TASK_RUNNING,
    TASK_SEQUENCE_CONFLICT,
    TASK_WAITING_FOR_INPUT,
    UNSUPPORTED_TASK_TYPE,
    BearingTaskRecord,
    PacketIngressResult,
    PacketRecord,
    TaskAck,
    TaskRecord,
)


@dataclass
class _TaskState:
    record: TaskRecord
    signature: tuple[Any, ...]
    accepted_ack: TaskAck
    packet_id_index: dict[tuple[str, str], tuple[int, str]]
    mutex: threading.Lock


class EdgeTaskIngress:
    """管理已接受任务，并把唯一、匹配的数据包交给校验缓存。"""

    def __init__(
        self,
        config: TaskIngressConfig,
        validation_cache: EdgeValidationCache,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        on_error: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        errors = config.validate()
        if errors:
            raise ValueError("任务接入配置无效: " + "; ".join(errors))
        if not hasattr(validation_cache, "process"):
            raise TypeError("validation_cache 必须提供process方法")
        self.config = config
        self._validation_cache = validation_cache
        self._clock_ns = clock_ns
        self._on_error = on_error or (lambda _: None)
        self._tasks: dict[tuple[str, str, str], _TaskState] = {}
        self._dispatch_index: dict[str, _TaskState] = {}
        self._tasks_mutex = threading.Lock()

    def register_task(self, dispatch: Mapping[str, Any]) -> TaskAck:
        """校验并原子登记一个设备任务；完全相同的重复派发幂等返回。"""
        received_at_ns = self._read_clock()
        normalized, reason = self._normalize_dispatch(dispatch)
        task_id = _task_id_from(dispatch)
        dispatch_id = _dispatch_id_from(dispatch)
        if reason is not None:
            return self._rejected_ack(
                task_id, reason, received_at_ns, dispatch_id
            )
        assert normalized is not None

        task_id = normalized["task_id"]
        dispatch_id = normalized["dispatch_id"]
        signature = normalized["signature"]
        task_keys = tuple(
            (normalized["device_id"], item["sender_id"], task_id)
            for item in normalized["assigned_bearings"]
        )
        with self._tasks_mutex:
            if dispatch_id is not None:
                existing_dispatch = self._dispatch_index.get(dispatch_id)
                if existing_dispatch is not None:
                    if existing_dispatch.signature == signature:
                        return existing_dispatch.accepted_ack
                    return self._rejected_ack(
                        task_id, TASK_CONFLICT, received_at_ns, dispatch_id
                    )
            existing_states = {
                id(state): state
                for key in task_keys
                if (state := self._tasks.get(key)) is not None
            }
            if existing_states:
                if (
                    len(existing_states) == 1
                    and next(iter(existing_states.values())).signature == signature
                ):
                    return next(iter(existing_states.values())).accepted_ack
                if not all(
                    state.record.task_status == TASK_REVOKED
                    for state in existing_states.values()
                ):
                    return self._rejected_ack(
                        task_id, TASK_CONFLICT, received_at_ns, dispatch_id
                    )

            acknowledged_at_ns = self._read_clock()
            bearings = {
                item["bearing_id"]: BearingTaskRecord(
                    task_id=task_id,
                    device_id=normalized["device_id"],
                    bearing_id=item["bearing_id"],
                    sender_id=item["sender_id"],
                )
                for item in normalized["assigned_bearings"]
            }
            record = TaskRecord(
                task_id=task_id,
                target_edge_node_id=normalized["target_edge_node_id"],
                task_type=normalized["task_type"],
                device_id=normalized["device_id"],
                expected_bearing_ids=normalized["expected_bearing_ids"],
                assigned_bearing_ids=tuple(bearings),
                dispatched_at_ns=normalized["dispatched_at_ns"],
                task_status=TASK_WAITING_FOR_INPUT,
                received_at_ns=received_at_ns,
                acknowledged_at_ns=acknowledged_at_ns,
                bearing_task_records=bearings,
                dispatch_id=dispatch_id,
            )
            ack = TaskAck(
                task_id=task_id,
                edge_node_id=self.config.edge_node_id,
                ack_status=ACK_ACCEPTED,
                reason_code=None,
                received_at_ns=received_at_ns,
                acknowledged_at_ns=acknowledged_at_ns,
                dispatch_id=dispatch_id,
            )
            state = _TaskState(
                record=record,
                signature=signature,
                accepted_ack=ack,
                packet_id_index={},
                mutex=threading.Lock(),
            )
            for key in task_keys:
                self._tasks[key] = state
            if dispatch_id is not None:
                self._dispatch_index[dispatch_id] = state
            return ack

    def receive_packet(self, raw_packet: Mapping[str, Any]) -> PacketIngressResult:
        """匹配一个原始包；仅有效新包会建立记录并调用校验缓存。"""
        received_at_ns = self._read_clock()
        identity = _packet_identity(raw_packet)
        if identity is None:
            return self._packet_rejected(
                INPUT_REFERENCE_MISMATCH, received_at_ns, raw_packet
            )

        with self._tasks_mutex:
            state = self._tasks.get(
                (identity["device_id"], identity["sender_id"], identity["task_id"])
            )
        if state is None:
            return self._packet_rejected(TASK_NOT_FOUND, received_at_ns, raw_packet)

        fingerprint = _content_fingerprint(raw_packet)
        with state.mutex:
            rejection = self._match_packet_locked(state, identity)
            if rejection is not None:
                return self._packet_rejected(rejection, received_at_ns, raw_packet)

            bearing = state.record.bearing_task_records[identity["bearing_id"]]
            existing = bearing.packet_records.get(identity["sequence_number"])
            if existing is not None:
                if existing.packet_id != identity["packet_id"]:
                    return self._packet_rejected(
                        TASK_SEQUENCE_CONFLICT, received_at_ns, raw_packet, existing
                    )
                if existing.content_fingerprint == fingerprint:
                    return PacketIngressResult(
                        status=INGRESS_DUPLICATE,
                        error_code=None,
                        received_at_ns=received_at_ns,
                        packet_record=copy.deepcopy(existing),
                        validated_packet=None,
                    )
                return self._packet_rejected(
                    PACKET_CONTENT_CONFLICT, received_at_ns, raw_packet, existing
                )

            packet_key = (identity["bearing_id"], identity["packet_id"])
            previous_identity = state.packet_id_index.get(packet_key)
            if previous_identity is not None and previous_identity != (
                identity["sequence_number"],
                fingerprint,
            ):
                return self._packet_rejected(
                    PACKET_CONTENT_CONFLICT, received_at_ns, raw_packet
                )

            raw_packet_ref = (
                identity["sender_id"],
                identity["task_id"],
                identity["sequence_number"],
            )
            packet_record = PacketRecord(
                **identity,
                packet_status=PACKET_PROCESSING,
                current_stage=STAGE_VALIDATION,
                received_at_ns=received_at_ns,
                content_fingerprint=fingerprint,
            )
            bearing.packet_records[identity["sequence_number"]] = packet_record
            state.packet_id_index[packet_key] = (
                identity["sequence_number"],
                fingerprint,
            )
            bearing.received_sequence_numbers.add(identity["sequence_number"])
            bearing.received_packet_count += 1
            if state.record.started_at_ns is None:
                state.record.started_at_ns = received_at_ns
            state.record.task_status = TASK_RUNNING
            if identity["sequence_number"] == EXPECTED_PACKET_COUNT:
                self._confirm_missing_locked(state.record, bearing)

        try:
            cache_result = self._validation_cache.process(
                raw_packet,
                ValidationCacheInvocationContext(received_at_ns),
                raw_packet_ref,
            )
        except Exception as exc:
            self._emit_error(RAW_CACHE_WRITE_FAILED, raw_packet, type(exc).__name__)
            cache_success = False
            error_code = RAW_CACHE_WRITE_FAILED
            validated_packet = None
        else:
            cache_success = bool(cache_result.status.success)
            error_code = cache_result.status.error_code
            validated_packet = cache_result.payload if cache_success else None

        with state.mutex:
            current = state.record.bearing_task_records[
                identity["bearing_id"]
            ].packet_records[identity["sequence_number"]]
            bearing = state.record.bearing_task_records[identity["bearing_id"]]
            if cache_success:
                current.current_stage = STAGE_DOWNSAMPLING
                current.raw_packet_ref = raw_packet_ref
            else:
                current.finished_at_ns = self._read_clock()
                current.current_stage = None
                current.error_code = error_code or RAW_CACHE_WRITE_FAILED
                bearing.terminal_packet_count += 1
                if current.error_code == RAW_CACHE_WRITE_FAILED:
                    current.packet_status = PACKET_PROCESSING_FAILED
                    bearing.processing_failed_count += 1
                else:
                    current.packet_status = PACKET_VALIDATION_REJECTED
                    bearing.validation_rejected_count += 1
            snapshot = copy.deepcopy(current)

        return PacketIngressResult(
            status=INGRESS_ACCEPTED,
            error_code=error_code,
            received_at_ns=received_at_ns,
            packet_record=snapshot,
            validated_packet=validated_packet,
        )

    def task_snapshot(
        self,
        task_id: str,
        *,
        device_id: Optional[str] = None,
        sender_id: Optional[str] = None,
    ) -> Optional[TaskRecord]:
        """返回不共享内部可变状态的任务快照。"""
        with self._tasks_mutex:
            if device_id is not None and sender_id is not None:
                state = self._tasks.get((device_id, sender_id, task_id))
            else:
                matches = {
                    id(state): state
                    for key, state in self._tasks.items()
                    if key[2] == task_id
                }
                state = next(iter(matches.values())) if len(matches) == 1 else None
        if state is None:
            return None
        with state.mutex:
            return copy.deepcopy(state.record)

    def packet_snapshot(
        self,
        task_id: str,
        bearing_id: str,
        sequence_number: int,
        *,
        device_id: Optional[str] = None,
        sender_id: Optional[str] = None,
    ) -> Optional[PacketRecord]:
        task = self.task_snapshot(
            task_id,
            device_id=device_id,
            sender_id=sender_id,
        )
        if task is None:
            return None
        bearing = task.bearing_task_records.get(bearing_id)
        if bearing is None:
            return None
        return bearing.packet_records.get(sequence_number)

    def revoke_dispatch(
        self, dispatch_id: str, *, reason_code: str, revoked_at_ns: Optional[int] = None
    ) -> bool:
        """幂等撤销一次派发；撤销后不再接受新包。"""
        if not _nonempty_string(dispatch_id) or not _nonempty_string(reason_code):
            raise ValueError("dispatch_id 和 reason_code 必须是非空字符串")
        with self._tasks_mutex:
            state = self._dispatch_index.get(dispatch_id)
        if state is None:
            return False
        revoked = self._read_clock() if revoked_at_ns is None else revoked_at_ns
        if not _positive_integer(revoked):
            raise ValueError("revoked_at_ns 必须是正整数")
        with state.mutex:
            if state.record.task_status == TASK_REVOKED:
                return True
            state.record.task_status = TASK_REVOKED
            state.record.revoked_at_ns = int(revoked)
            state.record.revoke_reason_code = reason_code
        return True

    def record_packet_completion(
        self,
        *,
        device_id: str,
        sender_id: str,
        task_id: str,
        bearing_id: str,
        sequence_number: int,
        output: Optional[Mapping[str, Any]],
        error_code: Optional[str],
        finished_at_ns: Optional[int] = None,
    ) -> bool:
        """把模型或降级路线的终态原子回填到对应包记录。"""
        key = (device_id, sender_id, task_id)
        with self._tasks_mutex:
            state = self._tasks.get(key)
        if state is None:
            return False
        finished = self._read_clock() if finished_at_ns is None else finished_at_ns
        if not _positive_integer(finished):
            raise ValueError("finished_at_ns 必须是正整数")
        with state.mutex:
            bearing = state.record.bearing_task_records.get(bearing_id)
            packet = bearing.packet_records.get(sequence_number) if bearing else None
            if bearing is None or packet is None:
                return False
            if packet.finished_at_ns is not None:
                return True
            packet.finished_at_ns = int(finished)
            packet.current_stage = None
            packet.error_code = error_code
            packet.edge_output = copy.deepcopy(dict(output)) if output is not None else None
            packet.packet_status = PACKET_SUCCEEDED if error_code is None else PACKET_PROCESSING_FAILED
            bearing.terminal_packet_count += 1
            if error_code is None:
                bearing.final_edge_count += 1
            else:
                bearing.processing_failed_count += 1
        return True

    def _normalize_dispatch(
        self, dispatch: Mapping[str, Any]
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if not isinstance(dispatch, Mapping):
            return None, INVALID_TASK
        task_id = dispatch.get("task_id")
        dispatch_id = dispatch.get("dispatch_id")
        target = dispatch.get("target_edge_node_id")
        task_type = dispatch.get("task_type")
        dispatched_at_ns = dispatch.get("dispatched_at_ns")
        if not _nonempty_string(task_id):
            return None, INVALID_TASK
        if dispatch_id is not None and not _nonempty_string(dispatch_id):
            return None, INVALID_TASK
        if not _nonempty_string(target):
            return None, INVALID_TASK
        if target != self.config.edge_node_id:
            return None, TARGET_NODE_MISMATCH
        if not _nonempty_string(task_type):
            return None, INVALID_TASK
        if task_type != SUPPORTED_TASK_TYPE:
            return None, UNSUPPORTED_TASK_TYPE
        if not _positive_integer(dispatched_at_ns):
            return None, INVALID_TASK

        input_ref = dispatch.get("input_ref")
        if not isinstance(input_ref, Mapping):
            return None, INVALID_TASK
        device_id = input_ref.get("device_id")
        expected = input_ref.get("expected_bearing_ids")
        assigned = input_ref.get("assigned_bearings")
        if not _nonempty_string(device_id):
            return None, INVALID_TASK
        if not isinstance(expected, (list, tuple)) or not expected:
            return None, INVALID_TASK
        if any(not _nonempty_string(item) for item in expected):
            return None, INVALID_TASK
        if len(set(expected)) != len(expected):
            return None, INVALID_TASK
        if not isinstance(assigned, (list, tuple)) or not assigned:
            return None, INVALID_TASK

        normalized_assigned: list[dict[str, Any]] = []
        bearing_ids: set[str] = set()
        sender_ids: set[str] = set()
        for item in assigned:
            if not isinstance(item, Mapping):
                return None, INVALID_TASK
            bearing_id = item.get("bearing_id")
            sender_id = item.get("sender_id")
            expected_count = item.get("expected_packet_count")
            if (
                not _nonempty_string(bearing_id)
                or not _nonempty_string(sender_id)
                or expected_count != EXPECTED_PACKET_COUNT
                or isinstance(expected_count, bool)
                or bearing_id not in expected
                or bearing_id in bearing_ids
                or sender_id in sender_ids
            ):
                return None, INVALID_TASK
            bearing_ids.add(bearing_id)
            sender_ids.add(sender_id)
            normalized_assigned.append(
                {
                    "bearing_id": bearing_id,
                    "sender_id": sender_id,
                    "expected_packet_count": EXPECTED_PACKET_COUNT,
                }
            )

        normalized_assigned.sort(key=lambda value: value["bearing_id"])
        expected_ids = tuple(sorted(expected))
        signature = (
            dispatch_id,
            task_id,
            target,
            task_type,
            device_id,
            expected_ids,
            tuple(
                (item["bearing_id"], item["sender_id"], EXPECTED_PACKET_COUNT)
                for item in normalized_assigned
            ),
            dispatched_at_ns,
        )
        return {
            "dispatch_id": dispatch_id,
            "task_id": task_id,
            "target_edge_node_id": target,
            "task_type": task_type,
            "device_id": device_id,
            "expected_bearing_ids": expected_ids,
            "assigned_bearings": normalized_assigned,
            "dispatched_at_ns": dispatched_at_ns,
            "signature": signature,
        }, None

    @staticmethod
    def _match_packet_locked(
        state: _TaskState, identity: Mapping[str, Any]
    ) -> Optional[str]:
        record = state.record
        if record.task_status not in (TASK_WAITING_FOR_INPUT, TASK_RUNNING):
            return TASK_NOT_ACCEPTING_INPUT
        bearing = record.bearing_task_records.get(identity["bearing_id"])
        if (
            bearing is None
            or identity["device_id"] != record.device_id
            or identity["sender_id"] != bearing.sender_id
        ):
            return INPUT_REFERENCE_MISMATCH
        sequence = identity["sequence_number"]
        if sequence < 1 or sequence > bearing.expected_packet_count:
            return INVALID_SEQUENCE_NUMBER
        return None

    def _confirm_missing_locked(
        self, task: TaskRecord, bearing: BearingTaskRecord
    ) -> None:
        bearing.end_sequence_received = True
        missing = tuple(
            sequence
            for sequence in range(1, bearing.expected_packet_count + 1)
            if sequence not in bearing.received_sequence_numbers
        )
        bearing.missing_sequence_numbers = missing
        bearing.missing_packet_count = len(missing)
        bearing.data_completeness = (
            COMPLETENESS_COMPLETE if not missing else COMPLETENESS_INCOMPLETE
        )
        if not missing:
            return
        confirmed_at_ns = self._read_clock()
        for sequence in missing:
            bearing.packet_records[sequence] = PacketRecord(
                device_id=task.device_id,
                bearing_id=bearing.bearing_id,
                task_id=task.task_id,
                packet_id=None,
                sender_id=bearing.sender_id,
                sequence_number=sequence,
                packet_status=PACKET_NOT_RECEIVED_STATUS,
                current_stage=None,
                received_at_ns=None,
                finished_at_ns=confirmed_at_ns,
                error_code=PACKET_NOT_RECEIVED,
            )
        bearing.terminal_packet_count += len(missing)

    def _rejected_ack(
        self,
        task_id: Optional[str],
        reason: str,
        received_at_ns: int,
        dispatch_id: Optional[str] = None,
    ) -> TaskAck:
        ack = TaskAck(
            task_id=task_id,
            edge_node_id=self.config.edge_node_id,
            ack_status=ACK_REJECTED,
            reason_code=reason,
            received_at_ns=received_at_ns,
            acknowledged_at_ns=self._read_clock(),
            dispatch_id=dispatch_id,
        )
        self._emit_error(
            reason,
            {"task_id": task_id, "dispatch_id": dispatch_id},
            "task_dispatch",
        )
        return ack

    def _packet_rejected(
        self,
        error_code: str,
        received_at_ns: int,
        raw_packet: object,
        existing: Optional[PacketRecord] = None,
    ) -> PacketIngressResult:
        source = raw_packet if isinstance(raw_packet, Mapping) else {}
        self._emit_error(error_code, source, "packet_ingress")
        return PacketIngressResult(
            status=INGRESS_REJECTED,
            error_code=error_code,
            received_at_ns=received_at_ns,
            packet_record=copy.deepcopy(existing),
            validated_packet=None,
        )

    def _read_clock(self) -> int:
        value = self._clock_ns()
        if not _positive_integer(value):
            raise ValueError("clock_ns 必须返回正整数")
        return int(value)

    def _emit_error(
        self, error_code: str, source: Mapping[str, Any], stage: str
    ) -> None:
        event = {
            key: source.get(key)
            for key in (
                "dispatch_id",
                "task_id",
                "device_id",
                "bearing_id",
                "sender_id",
                "packet_id",
                "sequence_number",
            )
        }
        event.update({"stage": stage, "error_code": error_code})
        try:
            self._on_error(event)
        except Exception:
            pass


def _packet_identity(packet: object) -> Optional[dict[str, Any]]:
    if not isinstance(packet, Mapping):
        return None
    result: dict[str, Any] = {}
    for field in ("device_id", "bearing_id", "task_id", "packet_id", "sender_id"):
        value = packet.get(field)
        if not _nonempty_string(value):
            return None
        result[field] = value
    sequence = packet.get("sequence_number")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    result["sequence_number"] = sequence
    return result


def _task_id_from(dispatch: object) -> Optional[str]:
    if not isinstance(dispatch, Mapping):
        return None
    value = dispatch.get("task_id")
    return value if _nonempty_string(value) else None


def _dispatch_id_from(dispatch: object) -> Optional[str]:
    if not isinstance(dispatch, Mapping):
        return None
    value = dispatch.get("dispatch_id")
    return value if _nonempty_string(value) else None


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_integer(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _content_fingerprint(value: object) -> str:
    digest = hashlib.sha256()
    _hash_value(digest, value)
    return digest.hexdigest()


def _hash_value(digest: Any, value: object) -> None:
    if isinstance(value, Mapping):
        digest.update(b"mapping{")
        for key in sorted(value, key=lambda item: (type(item).__name__, repr(item))):
            _hash_value(digest, key)
            _hash_value(digest, value[key])
        digest.update(b"}")
        return
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "biufc":
            _hash_numeric_array(digest, value)
        else:
            digest.update(b"array[")
            for item in value.flat:
                _hash_value(digest, item)
            digest.update(b"]")
        return
    if isinstance(value, (list, tuple)):
        try:
            array = np.asarray(value)
        except (TypeError, ValueError):
            array = None
        if array is not None and array.dtype.kind in "biufc":
            _hash_numeric_array(digest, array)
            return
        digest.update(b"sequence[")
        for item in value:
            _hash_value(digest, item)
        digest.update(b"]")
        return
    if value is None:
        digest.update(b"none")
    elif isinstance(value, (bool, np.bool_)):
        digest.update(b"bool1" if value else b"bool0")
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        digest.update(b"str" + struct.pack("!Q", len(encoded)) + encoded)
    elif isinstance(value, Real):
        number = float(value)
        if math.isnan(number):
            digest.update(b"number:nan")
        else:
            digest.update(b"number" + struct.pack("!d", number))
    else:
        encoded = repr(value).encode("utf-8")
        digest.update(
            b"object" + type(value).__name__.encode("utf-8") + b":" + encoded
        )


def _hash_numeric_array(digest: Any, value: np.ndarray) -> None:
    array = np.asarray(value)
    digest.update(b"numeric-array")
    digest.update(repr(tuple(array.shape)).encode("ascii"))
    if array.dtype.kind == "c":
        normalized = np.ascontiguousarray(array, dtype="<c16")
    else:
        normalized = np.ascontiguousarray(array, dtype="<f8")
    digest.update(normalized.tobytes())
