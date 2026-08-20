# -*- coding: utf-8 -*-
"""H3: 设备级建议生成线程——设备级最终诊断结果触发一次 LLM，而非逐包。

硬约束：在获得设备级最终诊断结果之后才触发 LLM。v12_flow 的四种闭合路径
（边缘自主闭合 / 轮次超时 / 云端复核回填 / 云仲裁修正）都会经 on_device_result
回调把 DeviceDecisionResult 非阻塞入队，由本线程消费一次并生成一条建议。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from suggestion_llm import SuggestionClient, build_suggestion_messages
from suggestion_rule import evaluate_suggestion

_logger = logging.getLogger(__name__)


def _risk_level_for_action_grade(grade: int) -> str:
    """按设备级最终动作等级推导风险等级（与 final_state 的 ACTION_TO_STATE 一致）。"""
    if grade >= 3:
        return "high"
    if grade == 2:
        return "medium"
    return "low"


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


class SuggestionWorker:
    """单线程消费设备级结果，生成并发布一条维护建议（规则 + LLM 翻译）。"""

    def __init__(
        self,
        *,
        llm_client: Optional[SuggestionClient],
        outbox: Any,
        publisher: Any,
        history_window: int = 10,
        queue_size: int = 256,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if history_window < 1:
            raise ValueError("history_window must be at least 1")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.llm_client = llm_client
        self.outbox = outbox
        self.publisher = publisher
        self.history_window = history_window
        self.clock_ns = clock_ns
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._mutex = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.processed_total = 0
        self.error_total = 0
        self.dropped_total = 0

    def submit(self, device_result: Any) -> bool:
        """非阻塞入队；队列满时丢弃并计数（建议是通知，明细查云端，不阻塞业务）。"""
        try:
            self._queue.put_nowait(device_result)
            return True
        except queue.Full:
            self.dropped_total += 1
            _logger.warning("suggestion worker 队列满, 丢弃建议(dropped=%d)", self.dropped_total)
            return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="edge-suggestion-worker", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        # 排空在逛建议：设备结果不会重新 emit，停止前必须处理完，
        # 否则 dispatcher 排空阶段产生的最后一批建议会永久丢失。
        while True:
            try:
                device_result = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._process(device_result)
            except Exception as exc:  # noqa: BLE001
                self.error_total += 1
                _logger.exception("suggestion worker 排空处理异常: %s", exc)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                device_result = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(device_result)
            except Exception as exc:  # noqa: BLE001
                self.error_total += 1
                _logger.exception("suggestion worker 处理设备级结果异常: %s", exc)
            finally:
                self._queue.task_done()

    def _process(self, device_result: Any) -> None:
        device_id = device_result.device_id
        final_state = device_result.final_state
        confidence = float(device_result.confidence)
        risk_level = _risk_level_for_action_grade(device_result.final_action_grade)

        # 1. 更新设备级历史（锁内）。
        with self._mutex:
            history = self._history.setdefault(device_id, [])
            history.append(
                {
                    "final_state": final_state,
                    "confidence": confidence,
                    "risk_level": risk_level,
                }
            )
            if len(history) > self.history_window:
                history[:] = history[-self.history_window:]
            snapshot = list(history)

        # 2. 规则引擎决策。
        rule_result = evaluate_suggestion(
            device_id=device_id,
            final_state=final_state,
            confidence=confidence,
            risk_level=risk_level,
            history=snapshot,
        )

        # 3. LLM 翻译为自然语言（不可达自动降级为 fallback 文本）。
        generated_by = "rule"
        if self.llm_client is not None:
            messages = build_suggestion_messages(
                device_id=device_id,
                label=final_state,
                confidence=confidence,
                risk_level=risk_level,
                suggestion_type=rule_result.suggestion_type,
                trend=rule_result.trend,
            )
            llm_result = self.llm_client.suggest(messages)
            suggestion_text = llm_result.text
            generated_by = "llm" if llm_result.success else "rule"
        else:
            suggestion_text = rule_result.reason + "。"

        # 4. 组装 13 字段精简契约（通知非数据源，明细以 decision_round_id+revision 查云端）。
        payload = {
            "result_id": "suggestion_%s_%s_%s_%s"
            % (
                device_id,
                device_result.task_id,
                device_result.decision_round_id,
                device_result.revision,
            ),
            "device_id": device_id,
            "task_id": device_result.task_id,
            "decision_round_id": device_result.decision_round_id,
            "device_result_revision": device_result.revision,
            "status": _enum_value(device_result.status),
            "final_state": final_state,
            "final_action_grade": device_result.final_action_grade,
            "confidence": confidence,
            "suggestion": suggestion_text,
            "suggestion_type": rule_result.suggestion_type,
            "priority": rule_result.priority,
            "generated_by": generated_by,
            "created_at_ns": self.clock_ns(),
        }

        # 5. 发布：优先持久化到 Outbox（维护轮次发送），未装配时直接 MQTT 发布。
        if self.outbox is not None:
            try:
                self.outbox.enqueue(payload)
            except Exception as exc:  # noqa: BLE001
                self.error_total += 1
                _logger.exception("suggestion outbox 入队失败: %s", exc)
            return
        if self.publisher is not None:
            try:
                self.publisher.publish(payload)
            except Exception as exc:  # noqa: BLE001
                self.error_total += 1
                _logger.exception("suggestion 直接发布失败: %s", exc)
