from __future__ import annotations

from types import SimpleNamespace
import sys

import edge_status_reporter.collectors as collectors
from edge_status_reporter.collectors import AcceleratorDetector, build_resource_collector
from edge_status_reporter.config import AcceleratorConfig, ResourceConfig


class FakeProcess:
    def __init__(self) -> None:
        self.cpu_values = iter((0.0, 240.0))

    def cpu_percent(self, interval=None) -> float:
        return next(self.cpu_values)

    def memory_info(self):
        return SimpleNamespace(rss=512 * 1024 * 1024)


class FakePsutil:
    def __init__(self) -> None:
        self.cpu_values = iter((0.0, 55.0))
        self.process = FakeProcess()

    def cpu_count(self, logical=True) -> int:
        return 8

    def cpu_percent(self, interval=None) -> float:
        return next(self.cpu_values)

    def virtual_memory(self):
        return SimpleNamespace(available=2048 * 1024 * 1024)

    def Process(self, process_id: int) -> FakeProcess:
        assert process_id > 0
        return self.process


def test_system_collector_warms_up_and_collects_system_resources() -> None:
    collector = build_resource_collector(ResourceConfig(), psutil_module=FakePsutil())
    collector.warm_up()
    snapshot = collector.collect()

    assert snapshot.logical_cpu_count == 8
    assert snapshot.cpu_utilization_percent == 55.0
    assert snapshot.memory_available_mb == 2048.0


def test_process_collector_normalizes_cpu_and_applies_memory_quota() -> None:
    collector = build_resource_collector(
        ResourceConfig(mode="process", logical_cpu_count=4, memory_limit_mb=1024.0),
        psutil_module=FakePsutil(),
    )
    collector.warm_up()
    snapshot = collector.collect()

    assert snapshot.logical_cpu_count == 4
    assert snapshot.cpu_utilization_percent == 60.0
    assert snapshot.memory_available_mb == 512.0


def test_accelerator_overrides_skip_probes_and_result_is_cached() -> None:
    calls = {"gpu": 0, "npu": 0}

    def gpu_probe() -> bool:
        calls["gpu"] += 1
        return False

    def npu_probe() -> bool:
        calls["npu"] += 1
        return True

    detector = AcceleratorDetector(
        AcceleratorConfig(gpu_available_override=True),
        gpu_probe=gpu_probe,
        npu_probe=npu_probe,
    )

    first = detector.detect()
    second = detector.detect()

    assert first is second
    assert first.gpu_available is True
    assert first.npu_available is True
    assert calls == {"gpu": 0, "npu": 1}


def test_system_collector_has_safe_fallback_when_psutil_is_not_installed(monkeypatch) -> None:
    def missing_psutil(name: str):
        assert name == "psutil"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(collectors.importlib, "import_module", missing_psutil)
    monkeypatch.setattr(collectors, "_native_available_memory_mb", lambda: 512.0)

    collector = build_resource_collector(ResourceConfig())
    collector.warm_up()
    snapshot = collector.collect()

    assert snapshot.logical_cpu_count >= 1
    assert snapshot.cpu_utilization_percent == 0.0
    assert snapshot.memory_available_mb == 512.0


def test_native_memory_query_failure_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(collectors.os, "name", "nt")
    monkeypatch.setattr(
        collectors.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(
                GlobalMemoryStatusEx=lambda value: (_ for _ in ()).throw(OSError("failed"))
            )
        ),
        raising=False,
    )

    assert collectors._native_available_memory_mb() == 0.0


def test_loaded_torch_npu_module_does_not_imply_available_hardware(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch_npu", SimpleNamespace())
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    assert collectors._loaded_torch_npu_available() is False
