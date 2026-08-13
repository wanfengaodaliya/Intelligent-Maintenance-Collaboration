# -*- coding: utf-8 -*-
"""边缘模型运行管线：单包感知 → 有界队列 → 模型/降级 → 单包结果。"""
from __future__ import annotations

import copy
import time
import uuid
from typing import Callable, Optional

from model_input_contract import validate_model_input

from .code_fallback import CodeFallbackRunner
from .config import EdgeModelConfig
from .contracts import (
    EXECUTION_CODE_FALLBACK,
    EXECUTION_LOCAL_MODEL,
    REASON_CODE_FALLBACK_FAILED,
    EdgeResult,
    PacketExecutionCompleted,
    PacketInferenceTask,
    PacketResult,
    RunRecord,
)
from .model_client import ModelClient
from .model_queue import InferenceWorker, ModelTaskQueue


class EdgeModelPipeline:
    def __init__(self, cfg: EdgeModelConfig, model_client: ModelClient,
                 fallback: CodeFallbackRunner,
                 on_run_record: Callable[[RunRecord], None],
                 on_packet_result: Callable[[PacketResult], None],
                 clock=time.monotonic,
                 on_packet_completed: Optional[Callable[[PacketExecutionCompleted], None]] = None,
                 clock_ns=time.time_ns):
        self.cfg = cfg
        self.model_client = model_client
        self.fallback = fallback
        self.on_run_record = on_run_record
        self.on_packet_result = on_packet_result
        self.on_packet_completed = on_packet_completed or (lambda _: None)
        self._clock = clock
        self._clock_ns = clock_ns

        self.queue = ModelTaskQueue(cfg.queue.max_waiting_requests,
                                    cfg.queue.full_policy, clock=clock)
        self.worker = InferenceWorker(
            self.queue,
            infer_fn=model_client.infer,
            cfg=cfg,
            on_model=self._on_model,
            on_fallback=self._on_fallback,
            clock=clock,
        )
        self.started = False

    def start(self):
        errors = self.cfg.validate()
        if errors:
            raise ValueError("边缘模型配置校验失败: " + "; ".join(errors))
        if self.cfg.diagnostic_backend == "local":
            self.started = True
            return
        url = (self.cfg.model_client.base_url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("模型服务地址非法: %r" % url)
        self.worker.start()
        self.started = True

    def stop(self, join_s: float = 5.0):
        if self.cfg.diagnostic_backend == "http":
            self.worker.stop(join_s=join_s)
        self.started = False

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        return self.queue.wait_until_idle(timeout_s)

    @property
    def max_observed_queued(self) -> int:
        return self.queue.max_observed_queued

    @property
    def queue_length(self) -> int:
        return self.queue.waiting_count

    def ingest(self, sender_id: str, perception: dict) -> str:
        """立即为当前 PerceptionResult 创建一个独立包级推理任务。"""

        if not self.started:
            raise RuntimeError("边缘模型管线未启动")
        task = self._make_task(sender_id, perception)
        if self.cfg.diagnostic_backend == "local":
            task.submit_ts = self._clock()
            self._run_local(task)
            return task.request_id
        result = self.queue.submit(task)
        for fallback_task, reason in result.fallback_tasks:
            self._run_fallback(fallback_task, reason)
        return task.request_id

    def _make_task(self, sender_id: str, perception: dict) -> PacketInferenceTask:
        validate_model_input(perception)
        identities = {}
        for field in ("device_id", "bearing_id", "task_id", "packet_id", "sender_id"):
            value = perception.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError("perception.%s 必须是非空字符串" % field)
            identities[field] = value
        sequence_number = perception.get("sequence_number")
        if isinstance(sequence_number, bool) or not isinstance(sequence_number, int) \
                or sequence_number < 1:
            raise ValueError("perception.sequence_number 必须是正整数")
        if identities["sender_id"] != sender_id:
            raise ValueError("sender_id 与 perception.sender_id 不一致")
        request_id = "%s:%s:%s:%s" % (
            identities["task_id"], sender_id, identities["packet_id"], uuid.uuid4().hex,
        )
        return PacketInferenceTask(
            request_id=request_id,
            device_id=identities["device_id"],
            bearing_id=identities["bearing_id"],
            task_id=identities["task_id"],
            packet_id=identities["packet_id"],
            sender_id=sender_id,
            sequence_number=sequence_number,
            perception=copy.deepcopy(perception),
            started_at_ns=self._clock_ns(),
        )

    def _run_fallback(self, task: PacketInferenceTask, reason: Optional[str],
                      queue_wait_ms: Optional[float] = None,
                      inference_ms: Optional[float] = None,
                      breaker_state: Optional[str] = None,
                      note: Optional[str] = None):
        try:
            edge = self.fallback.run(task)
            self._emit_result(task, edge, EXECUTION_CODE_FALLBACK, reason,
                              queue_wait_ms, inference_ms, breaker_state, note)
        except Exception as exc:  # noqa: BLE001
            self.on_run_record(RunRecord(
                request_id=task.request_id,
                device_id=task.device_id,
                bearing_id=task.bearing_id,
                task_id=task.task_id,
                packet_id=task.packet_id,
                sender_id=task.sender_id,
                sequence_number=task.sequence_number,
                execution_mode=EXECUTION_CODE_FALLBACK,
                fallback_reason=REASON_CODE_FALLBACK_FAILED,
                output_valid=False,
                breaker_state=breaker_state,
                note="model_route_reason=%s; fallback_error=%r" % (reason, exc),
            ))
            self.on_packet_completed(PacketExecutionCompleted(
                request_id=task.request_id,
                device_id=task.device_id,
                bearing_id=task.bearing_id,
                task_id=task.task_id,
                packet_id=task.packet_id,
                sender_id=task.sender_id,
                sequence_number=task.sequence_number,
                status="FAILED",
                error_code=REASON_CODE_FALLBACK_FAILED,
                started_at_ns=task.started_at_ns or self._clock_ns(),
                finished_at_ns=self._clock_ns(),
                edge=None,
                data_quality_score=0.0,
            ))

    def _run_local(self, task: PacketInferenceTask) -> None:
        try:
            edge = self.fallback.run(task)
            self._emit_result(
                task,
                edge,
                EXECUTION_LOCAL_MODEL,
                None,
                None,
                None,
                None,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            self.on_run_record(RunRecord(
                request_id=task.request_id,
                device_id=task.device_id,
                bearing_id=task.bearing_id,
                task_id=task.task_id,
                packet_id=task.packet_id,
                sender_id=task.sender_id,
                sequence_number=task.sequence_number,
                execution_mode=EXECUTION_LOCAL_MODEL,
                fallback_reason=REASON_CODE_FALLBACK_FAILED,
                output_valid=False,
                note="local_model_error=%r" % (exc,),
            ))
            self.on_packet_completed(PacketExecutionCompleted(
                request_id=task.request_id,
                device_id=task.device_id,
                bearing_id=task.bearing_id,
                task_id=task.task_id,
                packet_id=task.packet_id,
                sender_id=task.sender_id,
                sequence_number=task.sequence_number,
                status="FAILED",
                error_code=REASON_CODE_FALLBACK_FAILED,
                started_at_ns=task.started_at_ns or self._clock_ns(),
                finished_at_ns=self._clock_ns(),
                edge=None,
                data_quality_score=0.0,
            ))

    def _on_model(self, task: PacketInferenceTask, edge: EdgeResult,
                  queue_wait_ms: float, inference_ms: float, total_ms: float,
                  exceeded_total_timeout: bool, model_version: str):
        self._emit_result(task, edge, EXECUTION_LOCAL_MODEL, None,
                          queue_wait_ms, inference_ms,
                          self.worker.breaker_state, None,
                          total_ms=total_ms, exceeded=exceeded_total_timeout)

    def _on_fallback(self, task: PacketInferenceTask, reason: Optional[str],
                     queue_wait_ms: float, inference_ms: Optional[float],
                     breaker_state: Optional[str], note: Optional[str]):
        self._run_fallback(task, reason, queue_wait_ms, inference_ms,
                           breaker_state, note)

    def _emit_result(self, task: PacketInferenceTask, edge: EdgeResult, mode: str,
                     reason: Optional[str], queue_wait_ms: Optional[float],
                     inference_ms: Optional[float], breaker_state: Optional[str],
                     note: Optional[str], total_ms: Optional[float] = None,
                     exceeded: bool = False):
        if total_ms is None and task.submit_ts is not None:
            total_ms = (self._clock() - task.submit_ts) * 1000.0
        self.on_run_record(RunRecord(
            request_id=task.request_id,
            device_id=task.device_id,
            bearing_id=task.bearing_id,
            task_id=task.task_id,
            packet_id=task.packet_id,
            sender_id=task.sender_id,
            sequence_number=task.sequence_number,
            execution_mode=mode,
            fallback_reason=reason,
            edge_result=edge.edge_result,
            edge_risk_level=edge.edge_risk_level,
            confidence=edge.confidence,
            model_version=edge.model_version,
            queue_wait_ms=round(queue_wait_ms, 2) if queue_wait_ms is not None else None,
            inference_latency_ms=round(inference_ms, 2) if inference_ms is not None else None,
            total_latency_ms=round(total_ms, 2) if total_ms is not None else None,
            exceeded_total_timeout=exceeded,
            breaker_state=breaker_state,
            note=note,
        ))
        self.on_packet_result(PacketResult(
            device_id=task.device_id,
            bearing_id=task.bearing_id,
            task_id=task.task_id,
            packet_id=task.packet_id,
            sender_id=task.sender_id,
            sequence_number=task.sequence_number,
            edge=edge,
        ))
        self.on_packet_completed(PacketExecutionCompleted(
            request_id=task.request_id,
            device_id=task.device_id,
            bearing_id=task.bearing_id,
            task_id=task.task_id,
            packet_id=task.packet_id,
            sender_id=task.sender_id,
            sequence_number=task.sequence_number,
            status="SUCCEEDED",
            error_code=None,
            started_at_ns=task.started_at_ns or self._clock_ns(),
            finished_at_ns=self._clock_ns(),
            edge=edge,
            data_quality_score=_quality_score(task.perception),
        ))


def _quality_score(perception: dict) -> float:
    quality = perception.get("perception_quality") or {}
    flags = quality.get("flags")
    if quality.get("status") == "good" and flags == []:
        return 1.0
    if not isinstance(flags, list):
        return 0.0
    penalty = sum(0.2 if flag == "DEVICE_NOT_RUNNING" else 0.1 for flag in flags)
    return round(max(0.0, 1.0 - penalty), 3)
