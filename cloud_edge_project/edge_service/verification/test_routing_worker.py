# -*- coding: utf-8 -*-
"""H2: RoutingWorkerPool 行为验证——并发上限、乱序回放、异常护栏、
队列满降级、停机排空、未启动同步降级与计数器正确性。"""

from __future__ import annotations

import threading
import time

from edge_runtime.routing_worker import RoutingWorkerPool


def _make_pool(
    *,
    worker_count: int = 4,
    queue_size: int = 256,
    route_fn=None,
    replay_fn=None,
    clock_ns=time.time_ns,
):
    routed: list[int] = []
    replayed: list[int] = []
    if route_fn is None:

        def route_fn(completion, raw_packet, diagnosis_window):
            routed.append(completion)
            return {"route": "EDGE_PROVISIONAL"}

    if replay_fn is None:

        def replay_fn(replay):
            replayed.append(replay.seq)

    pool = RoutingWorkerPool(
        route_fn,
        replay_fn,
        worker_count=worker_count,
        queue_size=queue_size,
        clock_ns=clock_ns,
    )
    return pool, routed, replayed


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_unstarted_pool_submits_synchronously_and_counts_replay() -> None:
    """未启动时 submit 退化同步路由 + 同步回放，replayed_total 正确递增。"""
    pool, routed, replayed = _make_pool()
    assert pool.submit(1, None, None, None, None) is True
    assert pool.accepted_total == 1
    assert pool.replayed_total == 1  # 回归：不得出现 _replayed_total AttributeError
    assert routed == [1]
    assert replayed == [0]


def test_async_pool_routes_concurrently_within_worker_limit() -> None:
    """并发路由不超过 worker_count；全部 job 完成后 accepted==replayed==N。"""
    max_concurrent = 0
    current = 0
    counter_lock = threading.Lock()
    gate = threading.Event()

    def route_fn(completion, raw_packet, diagnosis_window):
        nonlocal max_concurrent, current
        with counter_lock:
            current += 1
            max_concurrent = max(max_concurrent, current)
        gate.wait(2.0)
        with counter_lock:
            current -= 1
        return {"route": "OK"}

    pool, _, replayed = _make_pool(worker_count=2, route_fn=route_fn)
    pool.start()
    try:
        for seq in range(6):
            assert pool.submit(seq, None, None, None, None) is True
        gate.set()
        _wait_for(lambda: pool.replayed_total == 6)
    finally:
        pool.stop()
    assert max_concurrent <= 2
    assert pool.accepted_total == 6
    assert pool.replayed_total == 6
    assert pool.failed_total == 0
    assert pool.overflow_total == 0
    assert pool.depth == 0
    assert pool.in_flight == 0


def test_out_of_order_completion_replays_in_submission_order() -> None:
    """worker 乱序完成时，回放仍按提交序号单调递增。"""
    # 池内 seq 从 1 开始（0 保留给未启动同步降级）；完成延迟不同：
    # seq 越大越早完成，制造乱序。
    delays = {1: 0.05, 2: 0.0, 3: 0.03}

    def route_fn(completion, raw_packet, diagnosis_window):
        time.sleep(delays.get(completion, 0.0))
        return {"route": "OK"}

    pool, _, replayed = _make_pool(worker_count=3, route_fn=route_fn)
    pool.start()
    try:
        for completion in (1, 2, 3):
            pool.submit(completion, None, None, None, None)
        _wait_for(lambda: pool.replayed_total == 3)
    finally:
        pool.stop()
    assert replayed == [1, 2, 3]  # 回放严格按提交顺序（seq 1-based）


def test_route_fn_exception_counts_failure_and_keeps_worker_alive() -> None:
    """route_fn 抛异常：failed_total 递增，job 仍回放（decision=None），worker 存活。"""

    def route_fn(completion, raw_packet, diagnosis_window):
        if completion == 2:
            raise RuntimeError("boom")
        return {"route": "OK"}

    pool, _, replayed = _make_pool(worker_count=2, route_fn=route_fn)
    pool.start()
    try:
        for completion in (1, 2, 3, 4):
            pool.submit(completion, None, None, None, None)
        _wait_for(lambda: pool.replayed_total == 4)
        assert pool.alive is True
    finally:
        pool.stop()
    assert pool.failed_total == 1
    assert replayed == [1, 2, 3, 4]  # 异常 job 不丢失，仍按序回放


def test_queue_full_degrades_with_false_and_overflow_counter() -> None:
    """队列满时 submit 返回 False 且 overflow_total 递增，不丢失已受理的 job。"""
    gate = threading.Event()
    first_taken = threading.Event()

    def route_fn(completion, raw_packet, diagnosis_window):
        first_taken.set()
        gate.wait(2.0)
        return {"route": "OK"}

    pool, _, replayed = _make_pool(worker_count=1, queue_size=2, route_fn=route_fn)
    pool.start()
    try:
        assert pool.submit(0, None, None, None, None) is True
        assert first_taken.wait(2.0)  # 等 worker 取走 job 0 并阻塞
        # worker 阻塞在 gate，队列容量 2：再放 2 个占满，第 3 个必须溢出。
        assert pool.submit(1, None, None, None, None) is True
        assert pool.submit(2, None, None, None, None) is True
        assert pool.submit(3, None, None, None, None) is False  # 队列满降级
        assert pool.overflow_total == 1
    finally:
        gate.set()
        pool.stop()
    assert pool.accepted_total == 3
    assert pool.replayed_total == 3
    assert replayed == [1, 2, 3]


def test_stop_drains_pending_and_replays_all() -> None:
    """停机排空：stop() 等待 worker 完成在途 job 并回放全部 pending。"""
    started = threading.Event()
    release = threading.Event()

    def route_fn(completion, raw_packet, diagnosis_window):
        started.set()
        release.wait(2.0)
        return {"route": "OK"}

    pool, _, replayed = _make_pool(worker_count=1, route_fn=route_fn)
    pool.start()
    for seq in range(3):
        assert pool.submit(seq, None, None, None, None) is True
    started.wait(2.0)
    release.set()
    pool.stop()  # 必须排空全部 3 个 job 并完成回放
    assert pool.accepted_total == 3
    assert pool.replayed_total == 3
    assert replayed == [1, 2, 3]
    assert pool.depth == 0
    assert pool.in_flight == 0
    assert pool.alive is False
    # 停止后新提交被拒绝。
    assert pool.submit(9, None, None, None, None) is False


def test_stop_without_start_is_noop() -> None:
    """未启动的池 stop() 安全无副作用。"""
    pool, _, _ = _make_pool()
    pool.stop()
    assert pool.alive is False
