# -*- coding: utf-8 -*-
"""SuggestionOutbox 行为验证：幂等发布、退避重试、死信与崩溃恢复。"""

from __future__ import annotations

import sqlite3

from edge_runtime.device_result_outbox import (
    DEAD_LETTER,
    PUBLISHED,
    RETRY_WAIT,
)
from edge_runtime.suggestion_outbox import SuggestionOutbox


def _suggestion(sequence: int = 1) -> dict:
    return {
        "result_id": "suggestion_task_01_bearing_01_packet_%03d" % sequence,
        "device_id": "device_01",
        "task_id": "task_01",
        "bearing_id": "bearing_01",
        "packet_id": "packet_%03d" % sequence,
        "suggestion": "建议关注。",
        "suggestion_type": "WATCH",
        "priority": "medium",
        "edge_result": "fault",
        "confidence": 0.85,
        "risk_level": "high",
    }


def test_suggestion_outbox_publishes_each_suggestion_once(tmp_path) -> None:
    published: list[dict] = []
    outbox = SuggestionOutbox(tmp_path / "edge.db", published.append, max_attempts=3)
    payload = _suggestion()

    assert outbox.enqueue(payload) is True
    assert outbox.enqueue(payload) is True  # 重复入队幂等
    assert outbox.run_once(0) == 1
    assert outbox.run_once(1) == 0  # 已发布版本不再发送

    assert len(published) == 1
    assert published[0]["result_id"] == payload["result_id"]
    health = outbox.health()
    assert health[PUBLISHED] == 1
    assert health["backlog"] == 0


def test_suggestion_outbox_retries_then_dead_letters(tmp_path) -> None:
    def failing(payload):
        raise RuntimeError("mqtt down")

    outbox = SuggestionOutbox(tmp_path / "edge.db", failing, max_attempts=2)
    outbox.enqueue(_suggestion())

    assert outbox.run_once(0) == 0
    assert outbox.health()[RETRY_WAIT] == 1
    # 退避窗口内不到期。
    assert outbox.run_once(100_000_000) == 0
    # 超过退避后第二次尝试失败，进入死信。
    assert outbox.run_once(10_000_000_000) == 0
    health = outbox.health()
    assert health[DEAD_LETTER] == 1
    assert health["backlog"] == 0


def test_suggestion_outbox_recovers_publishing_entries_on_startup(tmp_path) -> None:
    published: list[dict] = []
    database = tmp_path / "edge.db"
    first = SuggestionOutbox(database, published.append, max_attempts=5)
    first.enqueue(_suggestion())
    # 模拟进程在发送中途退出：条目停留在 PUBLISHING。
    connection = sqlite3.connect(database)
    connection.execute("UPDATE suggestion_outbox SET status='PUBLISHING'")
    connection.commit()
    connection.close()

    second = SuggestionOutbox(database, published.append, max_attempts=5)
    assert second.health()[RETRY_WAIT] == 1
    assert second.run_once(0) == 1
    assert len(published) == 1


def test_suggestion_outbox_rejects_payload_without_result_id(tmp_path) -> None:
    outbox = SuggestionOutbox(tmp_path / "edge.db", lambda payload: None)
    payload = _suggestion()
    payload.pop("result_id")

    try:
        outbox.enqueue(payload)
    except ValueError as error:
        assert "result_id" in str(error)
    else:
        raise AssertionError("missing result_id must be rejected")
