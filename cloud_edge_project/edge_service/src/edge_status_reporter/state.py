# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional

from .contracts import BusinessStatusSnapshot, ModelStatus


class EdgeApplicationState:
    def __init__(
        self,
        *,
        edge_node_id: str,
        model_version: str,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        queue_stale_ttl_seconds: float = 30.0,
    ) -> None:
        self.edge_node_id = edge_node_id
        self.model_version = model_version
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        # EDGE-2: STALE 缓存有效期（秒）。持续失败超过该时长后 STALE 降级为 FAILED，
        # 避免 last-known-good 无限期冒充“较新的真实值”。
        self._queue_stale_ttl_seconds = queue_stale_ttl_seconds
        self._lock = threading.Lock()
        self._last_task_activity_ns = 0
        self._load_status = "LOADED"
        # 运行时状态源：在运行时装配完成后可注入，替代固定值快照。
        self._queue_length_provider: Optional[Callable[[], int]] = None
        self._queue_breakdown_provider: Optional[Callable[[], Mapping[str, int]]] = None
        self._models_provider: Optional[Callable[[], tuple[ModelStatus, ...]]] = None
        self._activity_ns_provider: Optional[Callable[[], int]] = None
        # 队列最近一次成功值：提供方异常时作为 last-known-good 回退。
        self._last_known_queue_length: int | None = None
        self._last_known_queue_breakdown: Optional[dict[str, int]] = None
        # 最近一次成功采集的 monotonic 时间：用于 STALE → FAILED 的 TTL 判定。
        self._last_ok_at_monotonic: float | None = None
        # EDGE-1: 队列状态的可读原因（仅运维可观测，不进入上报合同）。
        self._queue_error: str | None = None

    @property
    def queue_error(self) -> str | None:
        """队列 measurement 失败的可读原因；None 表示状态 OK / 使用 last-known-good。"""
        with self._lock:
            return self._queue_error

    def attach_runtime_providers(
        self,
        *,
        queue_length_provider: Optional[Callable[[], int]] = None,
        queue_breakdown_provider: Optional[Callable[[], Mapping[str, int]]] = None,
        models_provider: Optional[Callable[[], tuple[ModelStatus, ...]]] = None,
        activity_ns_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        """注入真实运行时的队列、模型与活动状态源。"""
        with self._lock:
            if queue_length_provider is not None:
                self._queue_length_provider = queue_length_provider
            if queue_breakdown_provider is not None:
                self._queue_breakdown_provider = queue_breakdown_provider
            if models_provider is not None:
                self._models_provider = models_provider
            if activity_ns_provider is not None:
                self._activity_ns_provider = activity_ns_provider

    def touch_task_activity(self) -> None:
        value = self._clock_ns()
        with self._lock:
            self._last_task_activity_ns = max(self._last_task_activity_ns, value)

    def set_load_status(self, value: str) -> None:
        normalized = ModelStatus(self.model_version, value).load_status
        with self._lock:
            self._load_status = normalized

    def snapshot(self) -> BusinessStatusSnapshot:
        with self._lock:
            last_activity = self._last_task_activity_ns
            load_status = self._load_status
            queue_provider = self._queue_length_provider
            breakdown_provider = self._queue_breakdown_provider
            models_provider = self._models_provider
            activity_provider = self._activity_ns_provider
            last_known_length = self._last_known_queue_length
            last_known_breakdown = self._last_known_queue_breakdown
            last_ok_at = self._last_ok_at_monotonic
        # 状态是时效数据：提供方异常时回退到安全默认值，绝不影响业务主链路。
        try:
            extra_activity = int(activity_provider()) if activity_provider is not None else 0
        except Exception:
            extra_activity = 0
        last_activity = max(last_activity, max(extra_activity, 0))
        queue_length, queue_status, queue_breakdown = self._collect_queue(
            queue_provider,
            breakdown_provider,
            last_known_length,
            last_known_breakdown,
            now=self._monotonic(),
            last_ok_at=last_ok_at,
            stale_ttl_seconds=self._queue_stale_ttl_seconds,
        )
        if queue_status != "FAILED":
            with self._lock:
                self._queue_error = None
        with self._lock:
            if queue_status == "OK":
                self._last_known_queue_length = queue_length
                self._last_known_queue_breakdown = dict(queue_breakdown) if queue_breakdown else None
                self._last_ok_at_monotonic = self._monotonic()
        models = None
        if models_provider is not None:
            try:
                candidate = models_provider()
                if candidate:
                    models = tuple(candidate)
            except Exception:
                models = None
        if models is None:
            models = (ModelStatus(self.model_version, load_status),)
        return BusinessStatusSnapshot(
            edge_node_id=self.edge_node_id,
            queue_length=queue_length,
            models=models,
            last_task_activity_ns=last_activity,
            queue_measurement_status=queue_status,
            queue_breakdown=dict(queue_breakdown) if queue_breakdown is not None else None,
        )

    def _collect_queue(
        self,
        queue_provider: Optional[Callable[[], int]],
        breakdown_provider: Optional[Callable[[], Mapping[str, int]]],
        last_known_length: int | None,
        last_known_breakdown: Optional[dict[str, int]],
        *,
        now: float,
        last_ok_at: float | None,
        stale_ttl_seconds: float,
    ) -> tuple[int, str, dict[str, int] | None]:
        """采集队列长度；失败时优先 last-known-good，并明确标记状态。

        采集失败绝不能伪装成 queue_length=0 的“完全空闲”。
        EDGE-2：STALE 仅在 TTL 内可信；超过有效期后降级为 FAILED（0），
        避免 last-known-good 无限期冒充较新的真实值。
        """
        breakdown: dict[str, int] | None = None
        breakdown_failed = False
        if breakdown_provider is not None:
            try:
                raw = breakdown_provider()
                if isinstance(raw, Mapping):
                    breakdown = {
                        str(name): max(int(value), 0)
                        for name, value in raw.items()
                    }
            except Exception:
                breakdown = None
                breakdown_failed = True
        length: int | None = None
        if queue_provider is not None:
            try:
                length = max(int(queue_provider()), 0)
            except Exception:
                length = None
        if breakdown is not None:
            # 分桶之和即总队列长度；分桶与总长提供方同时存在时以分桶为准。
            return sum(breakdown.values()), "OK", breakdown
        if length is not None:
            return length, "OK", None
        # 两个提供方都失败（或均未注入却发生异常）：回退语义。
        if breakdown_failed or queue_provider is not None:
            # 仅当持有历史成功值且仍在 TTL 内时，才可信地标记 STALE。
            if (
                last_known_length is not None
                and last_ok_at is not None
                and (now - last_ok_at) <= stale_ttl_seconds
            ):
                return (
                    last_known_length,
                    "STALE",
                    dict(last_known_breakdown) if last_known_breakdown is not None else None,
                )
            # 从未成功，或 last-known-good 已过期：0 不再可信，明确 FAILED。
            self._queue_error = "queue_collection_failed"
            return 0, "FAILED", None
        # EDGE-1: 没有任何 Queue Provider 注入。队列长度“未知”，不是真实空队列。
        # 避免 0+OK 绕过 Scheduler 已经建立的 measurement_status 保守门控。
        self._queue_error = "queue_provider_unconfigured"
        return 0, "FAILED", None
