# -*- coding: utf-8 -*-
"""EDGE-1 可观测性测试。

覆盖：失败 WARNING 节流、恢复 INFO、多 target 独立跟踪、
health 字段、正常运行零噪音、target 抛异常兜底、线程安全、
以及 /health 最后一公里接线。
"""
from __future__ import annotations

import logging
import threading

import pytest

from edge_status_reporter.contracts import (
    AcceleratorSnapshot,
    BusinessStatusSnapshot,
    ModelStatus,
    NetworkSnapshot,
    ResourceSnapshot,
)
from edge_status_reporter.reporter import DeliveryTracker, EdgeStatusReporter
from edge_status_reporter.transport import SendOutcome

LOG = logging.getLogger("edge_status_reporter.reporter")


# ---------------------------------------------------------------------------
# 轻量采集/数据源 Fakes
# ---------------------------------------------------------------------------
class StatusSource:
    def snapshot(self) -> BusinessStatusSnapshot:
        return BusinessStatusSnapshot(
            edge_node_id="edge_01",
            queue_length=0,
            models=(ModelStatus("model-v1", "LOADED"),),
            last_task_activity_ns=10,
        )


class ResourceCollector:
    def __init__(self) -> None:
        self.warmed = False

    def warm_up(self) -> None:
        self.warmed = True

    def collect(self) -> ResourceSnapshot:
        return ResourceSnapshot(4, 10.0, 1024.0)


class AcceleratorDetector:
    def detect(self) -> AcceleratorSnapshot:
        return AcceleratorSnapshot(False, False)


class NetworkCollector:
    def collect(self) -> NetworkSnapshot:
        return NetworkSnapshot(120, 12.0, 8.0, 10.0, 0.01)


class FakeTarget:
    """返回固定 SendOutcome 的 target。"""

    def __init__(self, name: str, outcome: SendOutcome | None = None) -> None:
        self.name = name
        self._outcome = outcome
        self.calls = 0

    def send(self, payload: dict) -> SendOutcome:
        self.calls += 1
        if self._outcome is None:
            return SendOutcome(success=True, status_code=200, attempts=1)
        return self._outcome


class RaisingTarget(FakeTarget):
    def send(self, payload: dict) -> SendOutcome:
        self.calls += 1
        raise RuntimeError("programming bug in target")


_FAIL_OUTCOME = SendOutcome(success=False, status_code=503, error="HTTP_503", attempts=2)
_OK_OUTCOME = SendOutcome(success=True, status_code=200, attempts=1)


def _reporter(
    targets: tuple[FakeTarget, ...],
    *,
    interval: float = 10.0,
    clock_ns=None,
    monotonic=None,
) -> EdgeStatusReporter:
    return EdgeStatusReporter(
        status_source=StatusSource(),
        resource_collector=ResourceCollector(),
        accelerator_detector=AcceleratorDetector(),
        network_collector=NetworkCollector(),
        targets=targets,
        interval_seconds=interval,
        clock_ns=clock_ns or (lambda: 123),
        monotonic=monotonic or (lambda: 1.0),
    )


# ---------------------------------------------------------------------------
# DeliveryTracker：WARNING 节流 / 恢复 INFO
# ---------------------------------------------------------------------------
class _Clockbox:
    """可推进的单调时钟容器（秒）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.seconds = start

    def __call__(self) -> float:
        return self.seconds


def _tracker(*, monotonic=None, caplog=None) -> DeliveryTracker:
    return DeliveryTracker(
        name="scheduler",
        clock_ns=(lambda: int((monotonic() if monotonic else 0.0) * 1e9)),
        monotonic=monotonic or (lambda: 0.0),
    )


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def _infos(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.INFO]


def test_t1_throttles_failure_warnings(caplog) -> None:
    """T1：连续失败 31 次，WARNING 只在 1/3/10/30 出现，共 4 条。"""
    tracker = _tracker(caplog=caplog)
    with caplog.at_level(logging.WARNING):
        for _ in range(31):
            tracker.record(_FAIL_OUTCOME)

    warnings = _warnings(caplog)
    assert len(warnings) == 4
    counts = [r.args[1] for r in warnings]
    assert counts == [1, 3, 10, 30]
    assert tracker.consecutive_failures == 31


def test_t2_recovery_logs_once_with_previous_failures(caplog) -> None:
    """T2：失败 5 次后恢复，只输出 1 条 recovery INFO。"""
    box = _Clockbox(0.0)
    tracker = _tracker(monotonic=box)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            tracker.record(_FAIL_OUTCOME)
        box.seconds = 34.8
        tracker.record(_OK_OUTCOME)

    infos = _infos(caplog)
    assert len(infos) == 1
    message = infos[0].getMessage()
    assert "delivery recovered" in message
    assert "previous_failures=5" in message
    assert "failed_duration_seconds=34.8" in message
    assert tracker.consecutive_failures == 0


def test_t5_healthy_loop_is_silent(caplog) -> None:
    """T5：连续成功零噪音，无 WARNING / 无 recovery INFO。"""
    tracker = _tracker(caplog=caplog)
    with caplog.at_level(logging.INFO):
        for _ in range(20):
            tracker.record(_OK_OUTCOME)

    assert _warnings(caplog) == []
    assert _infos(caplog) == []
    assert tracker.consecutive_failures == 0
    assert tracker.status() == "OK"


# ---------------------------------------------------------------------------
# Reporter：多 target 独立、health、target 异常兜底
# ---------------------------------------------------------------------------
def test_t3_targets_track_failures_independently() -> None:
    """T3：Scheduler 一直失败、Cloud 一直成功 → 独立计数，overall degraded。"""
    scheduler = FakeTarget("scheduler", outcome=_OK_OUTCOME)
    cloud = FakeTarget("cloud", outcome=_OK_OUTCOME)
    reporter = _reporter((scheduler, cloud))

    # 先让 Scheduler 成功一次拿到 last_success，再持续失败 → DEGRADED。
    reporter.report_once()
    scheduler._outcome = _FAIL_OUTCOME
    for _ in range(5):
        reporter.report_once()

    health = reporter.health()
    assert health["running"] is False
    assert health["interval_seconds"] == 10.0

    by_name = {target["name"]: target for target in health["targets"]}
    assert by_name["scheduler"]["status"] == "DEGRADED"
    assert by_name["scheduler"]["consecutive_failures"] == 5
    assert by_name["scheduler"]["last_error"] == "HTTP_503"
    assert by_name["cloud"]["status"] == "OK"
    assert by_name["cloud"]["consecutive_failures"] == 0
    assert by_name["cloud"]["last_error"] is None
    assert health["status"] == "degraded"


def test_t3b_all_fail_gives_failed_overall() -> None:
    """Scheduler 从未成功 → status FAILED；overall=FAILED。"""
    scheduler = FakeTarget("scheduler", outcome=_FAIL_OUTCOME)
    reporter = _reporter((scheduler,))
    reporter.report_once()

    health = reporter.health()
    by_name = {target["name"]: target for target in health["targets"]}
    assert by_name["scheduler"]["status"] == "FAILED"
    assert by_name["scheduler"]["last_success_ns"] is None
    assert health["status"] == "failed"


def test_e4_dropped_report_count_tracks_per_target() -> None:
    """EDGE-4：每 target 独立累计最终交付失败，恢复后计数保留。

    Scheduler 失败 2 次 → scheduler.dropped_report_count=2；
    Cloud 全成功 → cloud.dropped_report_count=0；
    Scheduler 恢复 → consecutive_failures=0，但 dropped 仍为 2。
    """
    scheduler = FakeTarget("scheduler", outcome=_OK_OUTCOME)
    cloud = FakeTarget("cloud", outcome=_OK_OUTCOME)
    reporter = _reporter((scheduler, cloud))

    # 双 target 先各成功一次（拿到 last_success，便于后面判 DEGRADED）。
    reporter.report_once()

    # Scheduler 失败 2 次，Cloud 一直成功。
    scheduler._outcome = _FAIL_OUTCOME
    for _ in range(2):
        reporter.report_once()

    health = reporter.health()
    by_name = {t["name"]: t for t in health["targets"]}
    assert by_name["scheduler"]["dropped_report_count"] == 2
    assert by_name["scheduler"]["consecutive_failures"] == 2
    assert by_name["cloud"]["dropped_report_count"] == 0
    assert by_name["cloud"]["consecutive_failures"] == 0

    # Scheduler 恢复：consecutive 清零，累计 dropped 保留。
    scheduler._outcome = _OK_OUTCOME
    reporter.report_once()
    health = reporter.health()
    by_name = {t["name"]: t for t in health["targets"]}
    assert by_name["scheduler"]["consecutive_failures"] == 0
    assert by_name["scheduler"]["dropped_report_count"] == 2
    assert by_name["cloud"]["dropped_report_count"] == 0


def test_t4_health_exposes_required_fields() -> None:
    """T4：health 结构字段齐全。"""
    cloud = FakeTarget("cloud", outcome=_OK_OUTCOME)
    reporter = _reporter((cloud,))
    reporter.report_once()

    h = reporter.health()
    assert set(h) == {"running", "interval_seconds", "status", "targets"}
    assert h["status"] == "ok"
    target = h["targets"][0]
    for field in (
        "name",
        "status",
        "consecutive_failures",
        "last_success_ns",
        "last_failure_ns",
        "last_error",
        "last_failure_error",
    ):
        assert field in target, field
    assert target["name"] == "cloud"
    assert target["status"] == "OK"


def test_t6_target_exception_is_caught_and_loop_continues() -> None:
    """T6：target.send 抛异常 → Reporter 捕获计为失败，后台循环不中断。"""
    bad = RaisingTarget("scheduler")
    good = FakeTarget("cloud", outcome=_OK_OUTCOME)
    reporter = _reporter((bad, good))

    # 单次 report_once 不抛异常，两个 target 都被调用。
    report = reporter.report_once()

    assert report is not None
    assert bad.calls == 1
    assert good.calls == 1
    health = reporter.health()
    by_name = {target["name"]: target for target in health["targets"]}
    assert by_name["scheduler"]["consecutive_failures"] == 1
    assert by_name["scheduler"]["last_error"] == "RuntimeError"
    assert by_name["cloud"]["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# /health 最后一公里接线（真实 reporter.health + healthy 推导，走 HTTP）
# ---------------------------------------------------------------------------
def test_t10_health_endpoint_wiring() -> None:
    """真实 Go/Edge /health 语义：status_reporter 与 status_reporter_healthy。

    通过 FastAPI TestClient 调用 GET /health，覆盖：
    - 正常：healthy=true
    - Scheduler 失败：healthy=false，target 独立可见
    - 恢复：healthy=true
    """
    import warnings

    # 本项目运行于 httpx(非 httpx2)，starlette TestClient 的 httpx 回退会触发
    # StarletteDeprecationWarning；仅在调用方本地过滤该依赖性弃用警告，
    # 避免在 -W error 下把环境依赖噪音当作测试失败，不涉及屏蔽业务逻辑告警。
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".*httpx.*is deprecated.*")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

    def healthy(integration) -> bool:
        reporter = integration.reporter
        if reporter is None:
            return False
        return reporter.health()["status"] == "ok"

    scheduler = FakeTarget("scheduler")
    cloud = FakeTarget("cloud")
    reporter = _reporter((scheduler, cloud))
    integration = type("Integration", (), {"reporter": reporter})()

    app = FastAPI()
    app.state.integration = integration

    @app.get("/health")
    def health() -> dict:
        integration_ = app.state.integration
        reporter_ = integration_.reporter
        return {
            "status_reporter": reporter_.health() if reporter_ is not None else None,
            "status_reporter_healthy": healthy(integration_),
        }

    client = TestClient(app)

    # 正常
    reporter.report_once()
    body = client.get("/health").json()
    assert body["status_reporter_healthy"] is True
    assert body["status_reporter"]["status"] == "ok"

    # Scheduler 失败
    scheduler._outcome = _FAIL_OUTCOME
    for _ in range(3):
        reporter.report_once()
    body = client.get("/health").json()
    assert body["status_reporter_healthy"] is False
    assert body["status_reporter"]["status"] == "degraded"
    by_name = {t["name"]: t for t in body["status_reporter"]["targets"]}
    assert by_name["scheduler"]["status"] == "DEGRADED"
    assert by_name["cloud"]["status"] == "OK"

    # 恢复
    scheduler._outcome = _OK_OUTCOME
    reporter.report_once()
    body = client.get("/health").json()
    assert body["status_reporter_healthy"] is True
    assert body["status_reporter"]["status"] == "ok"


# ---------------------------------------------------------------------------
# 线程安全
# ---------------------------------------------------------------------------
def test_thread_safety_record_and_snapshot() -> None:
    """一个线程不断 record，另一线程不断 snapshot/health：无异常、计数不为负。"""
    import time

    scheduler = FakeTarget("scheduler")
    reporter = _reporter((scheduler,))
    tracker = reporter._trackers["scheduler"]

    stop = threading.Event()
    errors: list[str] = []

    def write_loop() -> None:
        while not stop.is_set():
            tracker.record(_FAIL_OUTCOME)
            tracker.record(_OK_OUTCOME)

    def read_loop() -> None:
        while not stop.is_set():
            snap = tracker.snapshot()
            if snap["consecutive_failures"] < 0:
                errors.append("negative failures in snapshot")
            if snap["status"] not in ("OK", "DEGRADED", "FAILED"):
                errors.append("invalid target status in snapshot")
            status = reporter.health()["status"]
            if status not in ("ok", "degraded", "failed"):
                errors.append("invalid health status")

    workers = [
        threading.Thread(target=write_loop, daemon=True),
        threading.Thread(target=write_loop, daemon=True),
    ]
    readers = [
        threading.Thread(target=read_loop, daemon=True),
        threading.Thread(target=read_loop, daemon=True),
    ]

    for worker in workers + readers:
        worker.start()

    time.sleep(0.2)
    stop.set()
    for worker in workers + readers:
        worker.join(2.0)

    assert errors == []
    snap = tracker.snapshot()
    assert snap["consecutive_failures"] >= 0
    assert snap["status"] in ("OK", "DEGRADED", "FAILED")


# ---------------------------------------------------------------------------
# EDGE-2：last_failure_error 历史失败原因保留
# ---------------------------------------------------------------------------
def test_edge2_T1_first_failure_sets_both_error_fields() -> None:
    """T1：第一次失败，last_error 与 last_failure_error 均有值。"""
    tracker = _tracker()
    tracker.record(_FAIL_OUTCOME)
    snap = tracker.snapshot()
    assert snap["last_error"] == "HTTP_503"
    assert snap["last_failure_error"] == "HTTP_503"
    assert snap["last_failure_ns"] is not None


def test_edge2_T2_recovery_keeps_history_clears_current() -> None:
    """T2：失败后恢复。last_error 清空，last_failure_error 保留，
    last_failure_ns 保留，consecutive_failures=0。"""
    box = _Clockbox(0.0)
    tracker = _tracker(monotonic=box)
    tracker.record(_FAIL_OUTCOME)
    box.seconds = 5.0
    tracker.record(_OK_OUTCOME)

    snap = tracker.snapshot()
    assert snap["consecutive_failures"] == 0
    assert snap["status"] == "OK"
    assert snap["last_error"] is None
    assert snap["last_failure_error"] == "HTTP_503"
    assert snap["last_failure_ns"] is not None


def test_edge2_T3_next_failure_overwrites_history() -> None:
    """T3：ConnectionError → 恢复 → HTTP_503。last_failure_error 最终为 HTTP_503。"""
    tracker = _tracker()
    tracker.record(SendOutcome(success=False, error="ConnectionError", attempts=3))
    tracker.record(_OK_OUTCOME)
    tracker.record(_FAIL_OUTCOME)

    snap = tracker.snapshot()
    assert snap["last_error"] == "HTTP_503"
    assert snap["last_failure_error"] == "HTTP_503"


def test_edge2_T4_two_targets_keep_independent_history() -> None:
    """T4：Scheduler 报 ConnectionError，Cloud 报 HTTP_503。各自 history 独立。"""
    scheduler = FakeTarget("scheduler", outcome=_OK_OUTCOME)
    cloud = FakeTarget("cloud", outcome=_OK_OUTCOME)
    reporter = _reporter((scheduler, cloud))
    # 先各成功一次拿到 last_success，便于恢复后判 OK。
    reporter.report_once()

    # Scheduler 失败为 ConnectionError，Cloud 失败为 HTTP_503。
    scheduler._outcome = SendOutcome(
        success=False, status_code=None, error="ConnectionError", attempts=3
    )
    cloud._outcome = _FAIL_OUTCOME
    for _ in range(2):
        reporter.report_once()

    health = reporter.health()
    by_name = {t["name"]: t for t in health["targets"]}
    assert (
        by_name["scheduler"]["last_failure_error"] == "ConnectionError"
    )
    assert by_name["cloud"]["last_failure_error"] == "HTTP_503"

    # 同时恢复：last_error 清零，last_failure_error 各自保留。
    scheduler._outcome = _OK_OUTCOME
    cloud._outcome = _OK_OUTCOME
    reporter.report_once()
    health = reporter.health()
    by_name = {t["name"]: t for t in health["targets"]}
    assert by_name["scheduler"]["consecutive_failures"] == 0
    assert by_name["cloud"]["consecutive_failures"] == 0
    assert by_name["scheduler"]["last_error"] is None
    assert by_name["cloud"]["last_error"] is None
    assert by_name["scheduler"]["last_failure_error"] == "ConnectionError"
    assert by_name["cloud"]["last_failure_error"] == "HTTP_503"


def test_edge2_T5_health_endpoint_exposes_last_failure_error() -> None:
    """T5：real GET /health 应答中能看到 targets[*].last_failure_error。

    走 FastAPI TestClient，验证最后一公里接线而非仅 tracker 内部对象。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".*httpx.*is deprecated.*")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

    scheduler = FakeTarget("scheduler", outcome=_OK_OUTCOME)
    reporter = _reporter((scheduler,))
    integration = type("Integration", (), {"reporter": reporter})()
    app = FastAPI()
    app.state.integration = integration

    @app.get("/health")
    def health() -> dict:
        reporter_ = app.state.integration.reporter
        return {"status_reporter": reporter_.health()}

    client = TestClient(app)

    reporter.report_once()  # 成功基线
    scheduler._outcome = _FAIL_OUTCOME
    reporter.report_once()  # 失败
    scheduler._outcome = _OK_OUTCOME
    reporter.report_once()  # 恢复

    body = client.get("/health").json()
    target = body["status_reporter"]["targets"][0]
    assert target["name"] == "scheduler"
    assert target["last_error"] is None
    assert target["last_failure_error"] == "HTTP_503"


# ---------------------------------------------------------------------------
# EDGE-3 Phase 1：Transport 错误分类必须足够区分，供事后归因
# ---------------------------------------------------------------------------
def test_edge3_p1_error_categories_stay_distinguishable() -> None:
    """EDGE-3 Phase 1：多类最终交付失败不得退化为同一条 error。

    至少区分 ConnectTimeout / ReadTimeout / ConnectionError / HTTP_4xx /
    HTTP_5xx / 其他异常。每个类别调用一次再恢复，最终 last_failure_error
    必须分别保留各自原始标签，证明基于错误分类的事后归因可行。
    """
    categories = {
        "ConnectTimeout": SendOutcome(success=False, error="ConnectTimeout", attempts=3),
        "ReadTimeout": SendOutcome(success=False, error="ReadTimeout", attempts=3),
        "ConnectionError": SendOutcome(
            success=False, status_code=None, error="ConnectionError", attempts=3
        ),
        "HTTP_400": SendOutcome(success=False, status_code=400, error="HTTP_400", attempts=1),
        "HTTP_503": SendOutcome(success=False, status_code=503, error="HTTP_503", attempts=2),
        "OSError": SendOutcome(success=False, status_code=None, error="OSError", attempts=3),
    }
    for label, outcome in categories.items():
        tracker = _tracker()
        tracker.record(outcome)
        snap = tracker.snapshot()
        assert snap["last_error"] == label, label
        assert snap["last_failure_error"] == label, label