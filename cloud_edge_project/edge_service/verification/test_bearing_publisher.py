# -*- coding: utf-8 -*-
"""H3-ASYNC: BearingPublisher 行为验证——异步发布、队列满丢弃(兑底语义)、
异常护栏、停机排空、未启动同步降级与计数器正确性。"""

from __future__ import annotations

import threading
import time

from edge_runtime.bearing_publisher import BearingPublisher


def _make_publisher(*, queue_size: int = 256, publish_fn=None):
    published: list = []
    if publish_fn is None:

        def publish_fn(completion):
            published.append(completion)

    publisher = BearingPublisher(publish_fn, queue_size=queue_size)
    return publisher, published


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_unstarted_publisher_publishes_synchronously() -> None:
    """未启动时 submit 退化同步发布，计数正确。"""
    publisher, published = _make_publisher()
    assert publisher.submit(1) is True
    assert publisher.accepted_total == 1
    assert publisher.published_total == 1
    assert published == [1]
    assert publisher.alive is False


def test_unstarted_publisher_does_not_count_failed_publish_as_success() -> None:
    def publish_fn(_completion):
        raise RuntimeError("boom")

    publisher, _ = _make_publisher(publish_fn=publish_fn)
    assert publisher.submit(1) is True
    assert publisher.published_total == 0
    assert publisher.failed_total == 1


def test_async_publisher_publishes_out_of_caller_thread() -> None:
    """启动后发布在独立线程执行，submit 立即返回（不阻塞调用方）。"""
    publisher, published = _make_publisher()
    publisher.start()
    try:
        assert publisher.submit(1) is True
        assert publisher.submit(2) is True
        _wait_for(lambda: publisher.published_total == 2)
    finally:
        publisher.stop()
    assert published == [1, 2]
    assert publisher.depth == 0
    assert publisher.overflow_total == 0
    assert publisher.failed_total == 0


def test_publish_fn_exception_counts_failure_and_keeps_thread_alive() -> None:
    """publish_fn 抛异常：failed_total 递增、线程存活、后续 job 继续发布。"""

    def publish_fn(completion):
        if completion == 2:
            raise RuntimeError("boom")

    publisher, _ = _make_publisher(publish_fn=publish_fn)
    publisher.start()
    try:
        for completion in (1, 2, 3):
            publisher.submit(completion)
        _wait_for(lambda: publisher.published_total == 2 and publisher.failed_total == 1)
        assert publisher.alive is True
    finally:
        publisher.stop()
    assert publisher.failed_total == 1


def test_queue_full_drops_job_with_overflow_counter() -> None:
    """队列满时 submit 返回 False 且 overflow_total 递增（由 H1-FIX 幂等兑底）。"""
    gate = threading.Event()
    first_taken = threading.Event()

    def publish_fn(completion):
        first_taken.set()
        gate.wait(2.0)

    publisher, _ = _make_publisher(queue_size=2, publish_fn=publish_fn)
    publisher.start()
    try:
        assert publisher.submit(1) is True
        assert first_taken.wait(2.0)  # 等线程取走 job 1 并阻塞
        assert publisher.submit(2) is True  # 占满队列
        assert publisher.submit(3) is True
        assert publisher.submit(4) is False  # 队列满：丢弃
        assert publisher.overflow_total == 1
    finally:
        gate.set()
        publisher.stop()
    assert publisher.accepted_total == 3


def test_stop_drains_pending_then_rejects_new_submissions() -> None:
    """停机排空：stop() 等待在途 job 全部发布完成，停止后新提交被拒绝。"""
    release = threading.Event()
    started = threading.Event()

    def publish_fn(completion):
        started.set()
        release.wait(2.0)

    publisher, _ = _make_publisher(publish_fn=publish_fn)
    publisher.start()
    for completion in (1, 2, 3):
        assert publisher.submit(completion) is True
    started.wait(2.0)
    release.set()
    publisher.stop()  # 必须排空全部 3 个 job
    assert publisher.published_total == 3
    assert publisher.depth == 0
    assert publisher.alive is False
    assert publisher.submit(9) is False  # 停止后拒绝


def test_stop_without_start_is_noop() -> None:
    """未启动的 publisher stop() 安全无副作用。"""
    publisher, _ = _make_publisher()
    publisher.stop()
    assert publisher.alive is False
