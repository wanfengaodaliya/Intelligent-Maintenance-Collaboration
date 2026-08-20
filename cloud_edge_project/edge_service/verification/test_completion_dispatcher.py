# -*- coding: utf-8 -*-
"""H1: CompletionDispatcher 行为验证——FIFO 顺序、溢出内联、异常护栏与排空。"""

from __future__ import annotations

import threading
import time

from edge_runtime.dispatcher import CompletionDispatcher


def test_dispatcher_processes_fifo_in_order() -> None:
    handled: list[int] = []
    dispatcher = CompletionDispatcher(handled.append, queue_size=8)
    dispatcher.start()
    try:
        for value in (1, 2, 3):
            dispatcher.submit(value)
        deadline = time.monotonic() + 2.0
        while len(handled) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        dispatcher.stop()
    assert handled == [1, 2, 3]


def test_dispatcher_overflow_falls_back_to_inline_without_loss() -> None:
    handled: list[int] = []
    release = threading.Event()

    def handler(value: int) -> None:
        handled.append(value)
        if value == 0:
            release.wait(2.0)  # 阻塞首项，使队列保持满以触发溢出

    dispatcher = CompletionDispatcher(handler, queue_size=1)
    dispatcher.start()
    try:
        dispatcher.submit(0)
        time.sleep(0.05)
        for value in (1, 2, 3):
            dispatcher.submit(value)  # 队列满 → 溢出内联执行
        assert dispatcher.overflow_total >= 1
    finally:
        release.set()
        dispatcher.stop()
    assert sorted(handled) == [0, 1, 2, 3]


def test_dispatcher_thread_survives_handler_exception() -> None:
    errors: list[Exception] = []

    def handler(value) -> None:
        raise ValueError("boom %s" % value)

    def on_error(item, exc) -> None:
        errors.append(exc)

    dispatcher = CompletionDispatcher(handler, queue_size=4, on_error=on_error)
    dispatcher.start()
    try:
        dispatcher.submit("a")
        dispatcher.submit("b")
        deadline = time.monotonic() + 2.0
        while len(errors) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert dispatcher.alive is True
    finally:
        dispatcher.stop()
    assert dispatcher.error_total >= 2
    assert len(errors) >= 2


def test_dispatcher_drains_queue_on_stop() -> None:
    handled: list[int] = []
    dispatcher = CompletionDispatcher(handled.append, queue_size=8)
    dispatcher.start()
    for value in range(5):
        dispatcher.submit(value)
    dispatcher.stop()  # 停止前排空队列
    assert sorted(handled) == [0, 1, 2, 3, 4]
