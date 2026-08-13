from __future__ import annotations

import threading

import pytest

from edge_status_reporter.contracts import (
    AcceleratorSnapshot,
    BusinessStatusSnapshot,
    ModelStatus,
    NetworkSnapshot,
    ResourceSnapshot,
)
from edge_status_reporter.reporter import EdgeStatusReporter


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


class Target:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.payloads: list[dict] = []
        self.called = threading.Event()

    def send(self, payload: dict) -> bool:
        self.payloads.append(payload)
        self.called.set()
        if self.fail:
            raise RuntimeError("target failed")
        return True


class BlockingTarget(Target):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.release = threading.Event()

    def send(self, payload: dict) -> bool:
        self.payloads.append(payload)
        self.called.set()
        self.release.wait(5.0)
        return True


class TimedTarget(Target):
    def __init__(self, name: str, timeout_seconds: float, retry_count: int) -> None:
        super().__init__(name)
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count


def _reporter(targets: tuple[Target, ...], interval: float = 10.0) -> tuple[EdgeStatusReporter, ResourceCollector]:
    resources = ResourceCollector()
    reporter = EdgeStatusReporter(
        status_source=StatusSource(),
        resource_collector=resources,
        accelerator_detector=AcceleratorDetector(),
        network_collector=NetworkCollector(),
        targets=targets,
        interval_seconds=interval,
        clock_ns=lambda: 123,
    )
    return reporter, resources


def test_reporter_sends_same_snapshot_and_isolates_target_failure() -> None:
    scheduler = Target("scheduler", fail=True)
    cloud = Target("cloud")
    reporter, _ = _reporter((scheduler, cloud))

    report = reporter.report_once()

    assert report is not None
    assert scheduler.payloads[0] is cloud.payloads[0]
    assert cloud.payloads[0]["reported_at_ns"] == 123


def test_reporter_thread_starts_immediately_and_stops_idempotently() -> None:
    target = Target("scheduler")
    reporter, resources = _reporter((target,))

    reporter.start()
    reporter.start()
    assert target.called.wait(1.0)
    assert reporter.running is True
    assert resources.warmed is True

    reporter.stop()
    reporter.stop()
    assert reporter.running is False


def test_stop_does_not_allow_restart_while_previous_thread_is_alive() -> None:
    target = BlockingTarget("scheduler")
    reporter, _ = _reporter((target,), interval=0.01)

    reporter.start()
    assert target.called.wait(1.0)
    reporter.stop()

    reporter.start()
    reporter_threads = [
        thread
        for thread in threading.enumerate()
        if thread.name == "edge-status-reporter" and thread.is_alive()
    ]
    assert len(reporter_threads) == 1

    target.release.set()
    reporter.stop()


def test_stop_timeout_budget_covers_all_target_attempts() -> None:
    targets = (
        TimedTarget("scheduler", timeout_seconds=0.5, retry_count=1),
        TimedTarget("cloud", timeout_seconds=0.5, retry_count=1),
    )
    reporter, _ = _reporter(targets, interval=0.01)

    assert reporter.stop_timeout_seconds >= 3.0


def test_failed_thread_start_does_not_leave_broken_thread_reference(monkeypatch) -> None:
    reporter, _ = _reporter(())

    class BrokenThread:
        def __init__(self, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("cannot start")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr("edge_status_reporter.reporter.threading.Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="cannot start"):
        reporter.start()
    reporter.stop()
    assert reporter.running is False


def test_report_loop_subtracts_work_time_from_interval() -> None:
    monotonic_values = iter((10.0, 10.3))
    reporter, _ = _reporter((), interval=1.0)
    reporter.monotonic = lambda: next(monotonic_values)
    waits: list[float] = []

    class StopAfterOneWait:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, timeout: float) -> None:
            waits.append(timeout)
            self.stopped = True

    reporter._stop = StopAfterOneWait()
    reporter._run()

    assert waits == [pytest.approx(0.7)]


def test_start_cannot_race_with_stop_cleanup() -> None:
    target = BlockingTarget("scheduler")
    reporter, _ = _reporter((target,), interval=0.01)
    reporter.start()
    assert target.called.wait(1.0)

    old_thread = reporter._thread
    original_join = old_thread.join
    join_completed = threading.Event()
    allow_stop_cleanup = threading.Event()

    def delayed_join(timeout=None) -> None:
        original_join(timeout)
        join_completed.set()
        allow_stop_cleanup.wait(1.0)

    old_thread.join = delayed_join
    stop_thread = threading.Thread(target=reporter.stop)
    stop_thread.start()
    target.release.set()
    assert join_completed.wait(1.0)

    reporter.start()
    allow_stop_cleanup.set()
    stop_thread.join(1.0)

    assert reporter.running is False
