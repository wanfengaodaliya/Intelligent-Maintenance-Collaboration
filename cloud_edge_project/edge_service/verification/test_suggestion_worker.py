# -*- coding: utf-8 -*-
"""H3: SuggestionWorker 行为验证——设备级触发一次、契约字段、revision 幂等键、风险推导。"""

from __future__ import annotations

import time
from types import SimpleNamespace

from edge_runtime.suggestion_worker import (
    SuggestionWorker,
    _risk_level_for_action_grade,
)
from suggestion_llm import SuggestionLlmResult


def _device_result(
    *,
    device_id: str = "device_01",
    revision: int = 1,
    final_state: str = "fault",
    final_action_grade: int = 3,
    confidence: float = 0.86,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=device_id,
        task_id="task_01",
        decision_round_id="round-01",
        revision=revision,
        status=SimpleNamespace(value="CORRECTED"),
        final_state=final_state,
        final_action_grade=final_action_grade,
        confidence=confidence,
    )


class _RecordingOutbox:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def enqueue(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return True


class _FakeLlm:
    def __init__(self) -> None:
        self.calls = 0

    def suggest(self, messages) -> SuggestionLlmResult:
        self.calls += 1
        return SuggestionLlmResult(text="建议安排检修。", success=True, fallback=False)


def _wait_until(predicate, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_risk_level_derivation() -> None:
    assert _risk_level_for_action_grade(0) == "low"
    assert _risk_level_for_action_grade(1) == "low"
    assert _risk_level_for_action_grade(2) == "medium"
    assert _risk_level_for_action_grade(3) == "high"
    assert _risk_level_for_action_grade(4) == "high"


def test_worker_emits_one_suggestion_per_device_result() -> None:
    outbox = _RecordingOutbox()
    worker = SuggestionWorker(llm_client=None, outbox=outbox, publisher=None)
    worker.start()
    try:
        worker.submit(_device_result())
        worker.submit(
            _device_result(revision=2, final_state="warning", final_action_grade=2)
        )
        _wait_until(lambda: len(outbox.payloads) == 2)
    finally:
        worker.stop()

    assert len(outbox.payloads) == 2
    first = outbox.payloads[0]
    # 13+ 字段精简契约关键字段齐全。
    for key in (
        "result_id", "device_id", "task_id", "decision_round_id",
        "device_result_revision", "status", "final_state",
        "final_action_grade", "confidence", "suggestion",
        "suggestion_type", "priority", "generated_by", "created_at_ns",
    ):
        assert key in first
    assert first["result_id"] == "suggestion_device_01_task_01_round-01_1"
    assert first["device_result_revision"] == 1
    assert first["final_state"] == "fault"
    assert first["final_action_grade"] == 3
    assert first["generated_by"] == "rule"  # 无 LLM 客户端时回退规则文本


def test_revision_changes_idempotency_key() -> None:
    outbox = _RecordingOutbox()
    worker = SuggestionWorker(llm_client=None, outbox=outbox, publisher=None)
    worker.start()
    try:
        worker.submit(_device_result(revision=1))
        worker.submit(_device_result(revision=2))  # 云仲裁修正 → 新建议
        _wait_until(lambda: len(outbox.payloads) == 2)
    finally:
        worker.stop()
    ids = [payload["result_id"] for payload in outbox.payloads]
    assert ids == [
        "suggestion_device_01_task_01_round-01_1",
        "suggestion_device_01_task_01_round-01_2",
    ]


def test_llm_called_once_per_device_result() -> None:
    outbox = _RecordingOutbox()
    llm = _FakeLlm()
    worker = SuggestionWorker(llm_client=llm, outbox=outbox, publisher=None)
    worker.start()
    try:
        worker.submit(_device_result(revision=1))
        worker.submit(_device_result(revision=2))
        _wait_until(lambda: len(outbox.payloads) == 2)
    finally:
        worker.stop()
    assert llm.calls == 2
    assert all(payload["generated_by"] == "llm" for payload in outbox.payloads)
