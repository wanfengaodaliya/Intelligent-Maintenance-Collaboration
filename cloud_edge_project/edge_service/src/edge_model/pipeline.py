# -*- coding: utf-8 -*-
"""边缘模型运行管线：感知 → 窗口聚合 → 有界队列 → 模型/降级 → 映射回每包。

正确性约束（P2）：
- 聚合器键 = (task_id, sender_id)：不同 task_id 的数据绝不混窗；
  同发送方切换任务 → 先关闭旧任务窗口；任务结束 → flush_task；
- 窗口级诊断 → 窗口内所有数据包（共享同一 EdgeResult，各包保留自身身份
  与业务时间；窗口元数据只进内部记录，不进外部接口）。

对外入口/回调：
    ingest(sender_id, perception, arrival_ts)   感知到达
    flush_task(task_id, sender_id) / flush() / stop()
    on_run_record(RunRecord)                    内部执行记录
    on_packet_result(PacketResult)              每包输出
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .code_fallback import CodeFallbackRunner
from .config import EdgeModelConfig
from .contracts import (
    EXECUTION_CODE_FALLBACK,
    EXECUTION_LOCAL_MODEL,
    EXECUTION_NONE,
    REASON_CODE_FALLBACK_FAILED,
    EdgeResult,
    PacketResult,
    RunRecord,
    WindowAggregate,
)
from .model_client import ModelClient
from .model_queue import InferenceWorker, ModelTaskQueue
from .window_aggregator import WindowAggregator


class EdgeModelPipeline:
    def __init__(self, cfg: EdgeModelConfig, model_client: ModelClient,
                 fallback: CodeFallbackRunner,
                 on_run_record: Callable[[RunRecord], None],
                 on_packet_result: Callable[[PacketResult], None],
                 clock=time.monotonic):
        self.cfg = cfg
        self.model_client = model_client
        self.fallback = fallback
        self.on_run_record = on_run_record
        self.on_packet_result = on_packet_result
        self._clock = clock

        self.queue = ModelTaskQueue(cfg.queue.max_waiting_requests, cfg.queue.full_policy, clock=clock)
        self.worker = InferenceWorker(
            self.queue,
            infer_fn=model_client.infer,
            fallback=fallback,
            cfg=cfg,
            on_model=self._on_model,
            on_fallback=self._on_fallback,
            clock=clock,
        )
        self._aggregators: Dict[Tuple[str, str], WindowAggregator] = {}
        self._sender_task: Dict[str, str] = {}  # sender_id → 当前 task_id
        self._agg_lock = threading.Lock()
        self.started = False

    # ---- 生命周期 ----
    def start(self):
        # 启动配置校验必须真正生效，失败即停止启动，不静默使用临时默认值
        errors = self.cfg.validate()
        if errors:
            raise ValueError("边缘模型配置校验失败: " + "; ".join(errors))
        url = (self.cfg.model_client.base_url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("模型服务地址非法: %r" % url)
        self.worker.start()
        self.started = True

    def stop(self, join_s: float = 5.0):
        self.worker.stop(join_s=join_s)

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        return self.queue.wait_until_idle(timeout_s)

    @property
    def max_observed_queued(self) -> int:
        return self.queue.max_observed_queued

    # ---- 感知入口 ----
    @staticmethod
    def _aggregator_key(task_id: str, sender_id: str) -> Tuple[str, str]:
        return (task_id, sender_id)

    def ingest(self, sender_id: str, perception: dict, arrival_ts: Optional[float] = None):
        task_id = str(perception.get("task_id") or "")
        to_flush: Optional[WindowAggregate] = None
        with self._agg_lock:
            prev_task = self._sender_task.get(sender_id)
            if prev_task is not None and prev_task != task_id:
                # 同发送方切换任务：先关闭旧任务的窗口，防止不同 task 混窗
                old_agg = self._aggregators.pop(self._aggregator_key(prev_task, sender_id), None)
                if old_agg is not None:
                    to_flush = old_agg.flush()
            self._sender_task[sender_id] = task_id
        if to_flush is not None:
            self._handle_closed(to_flush)
        agg = self._get_aggregator(task_id, sender_id)
        for window in agg.ingest(perception, arrival_ts):
            self._handle_closed(window)

    def flush_task(self, task_id: str, sender_id: str):
        """任务结束：关闭并移除该 (task_id, sender_id) 的窗口。"""
        key = self._aggregator_key(task_id, sender_id)
        with self._agg_lock:
            agg = self._aggregators.pop(key, None)
            if self._sender_task.get(sender_id) == task_id:
                self._sender_task.pop(sender_id, None)
        if agg is not None:
            w = agg.flush()
            if w is not None:
                self._handle_closed(w)

    def flush(self):
        """关闭所有发送方/任务的 active 窗口（收尾）。"""
        with self._agg_lock:
            aggs = list(self._aggregators.values())
        for agg in aggs:
            w = agg.flush()
            if w is not None:
                self._handle_closed(w)

    def _get_aggregator(self, task_id: str, sender_id: str) -> WindowAggregator:
        key = self._aggregator_key(task_id, sender_id)
        with self._agg_lock:
            agg = self._aggregators.get(key)
            if agg is None:
                agg = WindowAggregator(task_id, sender_id, self.cfg.window,
                                       field_mapping=self.cfg.feature_agg.field_mapping,
                                       clock=self._clock)
                self._aggregators[key] = agg
            return agg

    # ---- 窗口处理 ----
    def _handle_closed(self, window: WindowAggregate):
        if window.is_empty:
            self.on_run_record(RunRecord(
                sender_id=window.sender_id, window_id=window.window_id,
                task_id=window.task_id,
                window_start_ns=window.window_start_ns, window_end_ns=window.window_end_ns,
                sample_count=0, missing_ratio=1.0, sparse=False, is_empty=True,
                execution_mode=EXECUTION_NONE, fallback_reason=None, packet_count=0,
                late_dropped_count=window.late_dropped_count,
            ))
            return
        res = self.queue.submit(window)
        for w, reason in res.fallback_windows:
            self._run_fallback(w, reason)

    # ---- 降级 ----
    def _run_fallback(self, window: WindowAggregate, reason: Optional[str],
                      queue_wait_ms: Optional[float] = None,
                      inference_ms: Optional[float] = None,
                      breaker_state: Optional[str] = None,
                      note: Optional[str] = None):
        try:
            edge = self.fallback.run(window)
            self._emit_result(window, edge, EXECUTION_CODE_FALLBACK, reason,
                              queue_wait_ms, inference_ms, breaker_state, note)
        except Exception as exc:  # noqa: BLE001
            self.on_run_record(RunRecord(
                sender_id=window.sender_id, window_id=window.window_id,
                task_id=window.task_id,
                window_start_ns=window.window_start_ns, window_end_ns=window.window_end_ns,
                sample_count=window.sample_count, missing_ratio=window.missing_ratio,
                sparse=window.sparse, is_empty=False, packet_count=len(window.included_packets),
                execution_mode=EXECUTION_CODE_FALLBACK, fallback_reason=REASON_CODE_FALLBACK_FAILED,
                output_valid=False, late_dropped_count=window.late_dropped_count,
                breaker_state=breaker_state,
                note="model_route_reason=%s; fallback_error=%r" % (reason, exc),
            ))

    # ---- worker 回调 ----
    def _on_model(self, window: WindowAggregate, edge: EdgeResult,
                  queue_wait_ms: float, inference_ms: float, total_ms: float,
                  exceeded_total_timeout: bool, model_version: str):
        self._emit_result(window, edge, EXECUTION_LOCAL_MODEL, None,
                          queue_wait_ms, inference_ms,
                          self.worker.breaker_state, None,
                          total_ms=total_ms, exceeded=exceeded_total_timeout)

    def _on_fallback(self, window: WindowAggregate, reason: Optional[str],
                     queue_wait_ms: float, inference_ms: Optional[float],
                     breaker_state: Optional[str], note: Optional[str]):
        self._run_fallback(window, reason, queue_wait_ms, inference_ms, breaker_state, note)

    # ---- 结果产出 ----
    def _emit_result(self, window: WindowAggregate, edge: EdgeResult, mode: str,
                     reason: Optional[str], queue_wait_ms: Optional[float],
                     inference_ms: Optional[float], breaker_state: Optional[str],
                     note: Optional[str], total_ms: Optional[float] = None,
                     exceeded: bool = False):
        # 1) 内部运行记录
        self.on_run_record(RunRecord(
            sender_id=window.sender_id, window_id=window.window_id,
            task_id=window.task_id,
            window_start_ns=window.window_start_ns, window_end_ns=window.window_end_ns,
            sample_count=window.sample_count, missing_ratio=window.missing_ratio,
            sparse=window.sparse, is_empty=False, packet_count=len(window.included_packets),
            execution_mode=mode, fallback_reason=reason,
            edge_result=edge.edge_result, edge_risk_level=edge.edge_risk_level,
            confidence=edge.confidence, model_version=edge.model_version,
            queue_wait_ms=round(queue_wait_ms, 2) if queue_wait_ms is not None else None,
            inference_latency_ms=round(inference_ms, 2) if inference_ms is not None else None,
            total_latency_ms=round(total_ms, 2) if total_ms is not None else None,
            exceeded_total_timeout=exceeded, late_dropped_count=window.late_dropped_count,
            breaker_state=breaker_state, note=note,
        ))
        # 2) 窗口级 EdgeResult 映射给窗口内所有数据包（各包保留自身身份与业务时间）
        for ident in window.included_packets:
            self.on_packet_result(PacketResult(
                task_id=ident.get("task_id", window.task_id),
                packet_id=ident.get("packet_id", ""),
                sender_id=ident.get("sender_id", window.sender_id),
                sequence_number=ident.get("sequence_number", 0),
                edge=edge,
            ))
