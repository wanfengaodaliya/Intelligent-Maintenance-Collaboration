from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import logging
import sys

import pytest

import edge_status_reporter.collectors as collectors
from edge_status_reporter.collectors import (
    AcceleratorDetector,
    CgroupResourceCollector,
    build_resource_collector,
    detect_cgroup_version,
    CGROUP_V1,
    CGROUP_V2,
)
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


def test_native_memory_query_failure_returns_none(monkeypatch) -> None:
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

    # EDGE-3: native 采集失败返回 None，而非 0.0，
    # 由调用方据此区分“真实低内存”与“遥测失败”。
    assert collectors._native_available_memory_mb() is None


def test_native_system_collector_marks_memory_degraded_on_failure(monkeypatch) -> None:
    def missing_psutil(name: str):
        assert name == "psutil"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(collectors.importlib, "import_module", missing_psutil)
    monkeypatch.setattr(collectors, "_native_available_memory_mb", lambda: None)

    snapshot = build_resource_collector(ResourceConfig()).collect()

    # 保留 0.0 仅为兼容现有数值 schema，必须通过 status 告知“不是真实 0MiB”。
    assert snapshot.memory_available_mb == 0.0
    assert snapshot.memory_measurement_status == "DEGRADED"


def test_native_system_collector_keeps_ok_when_memory_measured(monkeypatch) -> None:
    def missing_psutil(name: str):
        assert name == "psutil"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(collectors.importlib, "import_module", missing_psutil)
    monkeypatch.setattr(collectors, "_native_available_memory_mb", lambda: 512.0)

    snapshot = build_resource_collector(ResourceConfig()).collect()

    assert snapshot.memory_available_mb == 512.0
    assert snapshot.memory_measurement_status == "OK"


def test_loaded_torch_npu_module_does_not_imply_available_hardware(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch_npu", SimpleNamespace())
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    assert collectors._loaded_torch_npu_available() is False


# ---------------------------------------------------------------------------
# EDGE-6：加速器探测 WARNING 节流（1/3/10 + 每 30 次）
# ---------------------------------------------------------------------------
def _gpu_only_detector(gpu_probe) -> AcceleratorDetector:
    """只让 GPU 走真实探测；NPU 用 override 跳过，probe_ttl=0 强制每次重测。"""
    return AcceleratorDetector(
        AcceleratorConfig(npu_available_override=True, probe_ttl_seconds=0.0),
        gpu_probe=gpu_probe,
        npu_probe=lambda: True,
    )


def _e6_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_e6_t1_throttles_probe_failure_warnings(caplog) -> None:
    """T1：连续探测异常 31 次，WARNING 只在 1/3/10/30，共 4 条。"""

    def boom() -> bool:
        raise RuntimeError("no accelerator")

    detector = _gpu_only_detector(boom)
    with caplog.at_level(logging.WARNING):
        for _ in range(31):
            detector.detect()

    warnings = _e6_warnings(caplog)
    assert len(warnings) == 4
    # 日志 args[2] 是“连续失败第 N 次”。
    assert [r.args[2] for r in warnings] == [1, 3, 10, 30]


def test_e6_t2_success_resets_streak_restarts_at_one(caplog) -> None:
    """T2：失败 5 次 → 一次成功 → 再次失败，新一轮立即从第 1 次 WARNING。"""
    state = {"fail": True}

    def probe() -> bool:
        if state["fail"]:
            raise RuntimeError("boom")
        return True

    detector = _gpu_only_detector(probe)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):      # 失败：WARNING 在第 1、3 次
            detector.detect()
        state["fail"] = False
        detector.detect()        # 成功：清零 streak
        state["fail"] = True
        detector.detect()        # 新一轮第 1 次失败 → WARNING

    warnings = _e6_warnings(caplog)
    assert [r.args[2] for r in warnings] == [1, 3, 1]


def test_e6_t3_healthy_probing_is_silent(caplog) -> None:
    """T3：连续成功探测不产生任何 WARNING（probe_ttl=0 每次真实探测）。"""
    detector = _gpu_only_detector(lambda: True)
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            detector.detect()

    assert _e6_warnings(caplog) == []
    # 成功路径始终不进入失败 streak。
    assert detector._probe_failure_streak == {"GPU": 0, "NPU": 0}


# ---------------------------------------------------------------------------
# ENV-3：cgroup/container 资源采集模式
# ---------------------------------------------------------------------------
def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _v2_root(tmp_path: Path) -> Path:
    root = tmp_path / "cgroup_v2"
    _write(root / "cgroup.controllers", "cpu memory")
    _write(root / "cpu.max", "100000 100000")
    _write(root / "cpu.stat", "usage_usec 100000\nuser_usec 90000\nsystem_usec 10000\n")
    _write(root / "memory.max", str(1024 * 1024 * 1024))  # 1GB
    _write(root / "memory.current", str(400 * 1024 * 1024))  # 400MB
    return root


class _SeqClock:
    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._i = 0

    def __call__(self) -> float:
        value = self._values[self._i]
        self._i = min(self._i + 1, len(self._values) - 1)
        return value


def test_env3_t1_v2_version_detection(tmp_path) -> None:
    """T1：v2 检测优先；仅 v2 标志缺失时才探测 v1。"""
    v2 = _v2_root(tmp_path)
    assert detect_cgroup_version(v2) == CGROUP_V2

    v1 = tmp_path / "cgroup_v1"
    _write(v1 / "cgroup" / "cpu" / "cpu.cfs_quota_us", "-1")
    _write(v1 / "cgroup" / "cpu" / "cpu.cfs_period_us", "100000")
    assert detect_cgroup_version(v1) == CGROUP_V1

    none = tmp_path / "cgroup_none"
    assert detect_cgroup_version(none) is None


def test_env3_t2_cpu_quota_effective_count(tmp_path) -> None:
    """T2：CPU quota/period → effective_cpu_count（1 CPU 与 2 CPU）。"""
    root = _v2_root(tmp_path)
    snap1 = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap1.logical_cpu_count == 1

    _write(root / "cpu.max", "200000 100000")
    snap2 = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap2.logical_cpu_count == 2


def test_env3_t3_cpu_utilization_incremental(tmp_path) -> None:
    """T3：两次 CPU usage 差值 / 墙钟 / effective_cpu_count → 0~100%。"""
    root = _v2_root(tmp_path)
    # 2 CPU；1 秒内增加 2s 的 CPU 使用 → 100%（2s/1s/2 核）。
    collector = CgroupResourceCollector(
        ResourceConfig(mode="cgroup"),
        cgroup_root=root,
        monotonic=_SeqClock([10.0, 11.0]),
    )
    _write(root / "cpu.max", "200000 100000")
    _write(root / "cpu.stat", "usage_usec 0\n")
    collector._prev_usage_usec = 0
    collector._prev_monotonic = 10.0
    collector._prev_usage_usec = None
    # 首轮无基准：返回 0 + 不失败
    snap_first = collector.collect()
    assert snap_first.cpu_utilization_percent == 0.0
    assert snap_first.cpu_measurement_status == "OK"
    assert snap_first.logical_cpu_count == 2
    # 第二轮：usage 从 0 -> 2_000_000 usec（2 秒），墙钟 10 -> 11（1 秒），2 核
    _write(root / "cpu.stat", "usage_usec 2000000\n")
    collector._prev_usage_usec = 0
    collector._prev_monotonic = 10.0
    snap = collector.collect()
    assert snap.cpu_utilization_percent == 100.0


def test_env3_t4_memory_available_calculation(tmp_path) -> None:
    """T4：memory_available = limit - current（1GB - 400MB ≈ 624MB）。"""
    root = _v2_root(tmp_path)
    snap = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap.memory_available_mb == pytest.approx(1024.0 - 400.0, abs=0.5)
    assert snap.memory_measurement_status == "OK"


def test_env3_t5_unlimited_memory_max_is_degraded(tmp_path) -> None:
    """T5：memory.max=max 不能得到巨大伪值，必须标记 degraded。"""
    root = _v2_root(tmp_path)
    _write(root / "memory.max", "max")
    snap = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap.memory_measurement_status == "DEGRADED"
    # 不能是 max - current 的伪大值：保守返回 0。
    assert snap.memory_available_mb == 0.0


def test_env3_t6_read_failure_never_returns_0_ok(tmp_path) -> None:
    """T6：cgroup 文件读取失败 → FAILED，绝不返回 0+OK。"""
    root = _v2_root(tmp_path)
    # 删除 memory.max → 读取失败
    (root / "memory.max").unlink()
    snap = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap.memory_measurement_status == "FAILED"
    assert snap.memory_available_mb == 0.0


def test_env3_t6b_cpu_read_failure_is_degraded_not_ok(tmp_path) -> None:
    """T6b：内存 OK 但 CPU 文件缺失 → CPU degraded，memory 仍正常。"""
    root = _v2_root(tmp_path)
    (root / "cpu.stat").unlink()
    (root / "cpu.max").unlink()
    snap = CgroupResourceCollector(ResourceConfig(mode="cgroup"), cgroup_root=root).collect()
    assert snap.cpu_measurement_status == "DEGRADED"
    assert snap.memory_measurement_status == "OK"


def test_env3_config_accepts_cgroup_mode(tmp_path) -> None:
    """T7：ResourceConfig 接受 cgroup 模式且无需额外配额。"""
    cfg = ResourceConfig(mode="cgroup")
    collector = build_resource_collector(cfg)  # 构造不抛错
    assert isinstance(collector, CgroupResourceCollector)


def test_env3_env_parses_cgroup_mode(tmp_path, monkeypatch) -> None:
    """T8：EDGE_STATUS_RESOURCE_MODE=cgroup 从环境变量正确解析。"""
    from edge_status_reporter.config import EdgeStatusReporterConfig

    config = EdgeStatusReporterConfig.from_env(
        default_model_version="bearing-v1",
        environ={"EDGE_STATUS_RESOURCE_MODE": "cgroup"},
    )
    assert config.resource.mode == "cgroup"
