# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import AcceleratorConfig, ResourceConfig
from .contracts import AcceleratorSnapshot, ResourceSnapshot


MEBIBYTE = 1024 * 1024

# ENV-3: cgroup 资源采集支持的版本。
CGROUP_V2 = "v2"
CGROUP_V1 = "v1"


def _clamp_percent(value: object) -> float:
    return min(max(float(value), 0.0), 100.0)


class ResourceCollector(Protocol):
    def warm_up(self) -> None: ...

    def collect(self) -> ResourceSnapshot: ...


class SystemResourceCollector:
    def __init__(self, psutil_module: Any) -> None:
        self.psutil = psutil_module

    def warm_up(self) -> None:
        self.psutil.cpu_percent(interval=None)

    def collect(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            logical_cpu_count=max(int(self.psutil.cpu_count(logical=True) or 1), 1),
            cpu_utilization_percent=_clamp_percent(self.psutil.cpu_percent(interval=None)),
            memory_available_mb=max(float(self.psutil.virtual_memory().available) / MEBIBYTE, 0.0),
        )


class NativeSystemResourceCollector:
    def warm_up(self) -> None:
        return None

    def collect(self) -> ResourceSnapshot:
        memory_available_mb = _native_available_memory_mb()
        return ResourceSnapshot(
            logical_cpu_count=max(int(os.cpu_count() or 1), 1),
            # psutil 缺失时 CPU 利用率无法采集：0 不是“完全空闲”，
            # 必须以 DEGRADED 标记，避免下游误读。
            cpu_utilization_percent=0.0,
            # native 内存采集失败时为 None；保留 0.0 仅用于兼容现有数值 schema，
            # 必须通过 memory_measurement_status 告知下游“这个 0 不是真实 0MB”。
            memory_available_mb=0.0 if memory_available_mb is None else memory_available_mb,
            cpu_measurement_status="DEGRADED",
            memory_measurement_status="DEGRADED" if memory_available_mb is None else "OK",
        )


class ProcessResourceCollector:
    def __init__(self, config: ResourceConfig, psutil_module: Any) -> None:
        self.config = config
        self.process = psutil_module.Process(os.getpid())

    def warm_up(self) -> None:
        self.process.cpu_percent(interval=None)

    def collect(self) -> ResourceSnapshot:
        logical_cpu_count = int(self.config.logical_cpu_count or 1)
        memory_limit_mb = float(self.config.memory_limit_mb or 0.0)
        memory_used_mb = float(self.process.memory_info().rss) / MEBIBYTE
        return ResourceSnapshot(
            logical_cpu_count=logical_cpu_count,
            cpu_utilization_percent=_clamp_percent(
                self.process.cpu_percent(interval=None) / logical_cpu_count
            ),
            memory_available_mb=max(memory_limit_mb - memory_used_mb, 0.0),
        )


def _read_line(path: Path) -> str:
    """读取 cgroup 文件并返回去除首尾空白的一行内容。"""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def detect_cgroup_version(root: Path | None = None) -> str | None:
    """检测 cgroup 版本：优先 v2；仅当 v2 不存在时才探测 v1。"""
    root = root or Path("/sys/fs/cgroup")
    if (root / "cgroup.controllers").exists():
        return CGROUP_V2
    if (root / "cgroup" / "cpu").exists() or (root / "cpu").exists():
        return CGROUP_V1
    return None


def _parse_cpu_quota_period(root: Path, version: str) -> tuple[int | None, int | None]:
    """返回 (quota, period)。quota 为 None 表示无 CPU 配额（不限）。"""
    if version == CGROUP_V2:
        raw = _read_line(root / "cpu.max")
        parts = raw.split()
        period = int(parts[1]) if len(parts) >= 2 else 100000
        quota_raw = parts[0]
        if quota_raw == "max":
            return None, period
        return int(quota_raw), period
    # v1
    quota = int(_read_line(root / "cpu" / "cpu.cfs_quota_us"))
    period = int(_read_line(root / "cpu" / "cpu.cfs_period_us"))
    if quota == -1:
        return None, period
    return quota, period


def _count_cpuset_cpus(root: Path, version: str) -> int | None:
    """读取 cpuset 允许的 CPU 数量；读取失败或不能解析时返回 None。"""
    if version == CGROUP_V2:
        path = root / "cpuset.cpus.effective"
    else:
        path = root / "cpuset.cpus"
    try:
        raw = _read_line(path)
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    count = 0
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            start_s, _, end_s = piece.partition("-")
            try:
                count += int(end_s) - int(start_s) + 1
            except ValueError:
                return None
        else:
            try:
                count += 1
            except ValueError:
                return None
    return count or None


class CgroupResourceCollector:
    """cgroup/container 视角的资源采集。

    语义（与 system/process 模式并列，不改变这两者的原语义）：
      - cgroup 模式下 logical_cpu_count 取容器真正被允许使用的 CPU 上限
        （quota/period，配额无限时回退 cpuset / 系统可见 CPU）；
      - CPU 利用率基于 cgroup CPU usage 的两次差值 / 墙钟 / effective_cpu_count，
        归一化为 0~100%，与 Scheduler 合同一致；
      - memory_available = memory.limit - memory.current；
        无限 memory.max（字符串 "max" / v1 无限制大数）时按 degraded 标记，不做伪计算；
      - 任一 cgroup 文件读取失败时绝不以 0+OK 上报，必须携带不可信
        measurement_status（DEGRADED/FAILED）。
    """

    def __init__(
        self,
        config: ResourceConfig,
        *,
        cgroup_root: str | Path | None = None,
        psutil_module: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._root = Path(cgroup_root) if cgroup_root else Path("/sys/fs/cgroup")
        self._version = detect_cgroup_version(self._root)
        self._monotonic = monotonic
        # CPU 利用率需要相邻两次采样计算增量。
        self._prev_usage_usec: int | None = None
        self._prev_monotonic: float | None = None

    def warm_up(self) -> None:
        usage = self._read_cpu_usage_usec()
        self._prev_usage_usec = usage
        self._prev_monotonic = self._monotonic()

    def collect(self) -> ResourceSnapshot:
        cpu_status = "OK"
        memory_status = "OK"

        logical_cpu_count, failure = self._effective_cpu_count()
        if failure:
            cpu_status = "DEGRADED"

        cpu_utilization, cpu_failure = self._cpu_utilization(logical_cpu_count)
        if cpu_failure:
            cpu_status = "DEGRADED"

        memory_available_mb, mem_degraded, memory_failure = self._memory_available_mb()
        if memory_failure:
            memory_status = "FAILED"
        elif mem_degraded:
            memory_status = "DEGRADED"

        return ResourceSnapshot(
            logical_cpu_count=logical_cpu_count,
            cpu_utilization_percent=_clamp_percent(cpu_utilization),
            memory_available_mb=max(memory_available_mb, 0.0),
            cpu_measurement_status=cpu_status,
            memory_measurement_status=memory_status,
        )

    # -- CPU -------------------------------------------------------------
    def _effective_cpu_count(self) -> tuple[int, bool]:
        """返回 (effective_cpu_count, degraded)。degraded=True 表示该值不可完全可信。"""
        if self._version is None:
            # 非容器/无 cgroup：回退宿主可见 CPU，标记 degraded（非 cgroup 视角）。
            return max(int(os.cpu_count() or 1), 1), True
        try:
            quota, period = _parse_cpu_quota_period(self._root, self._version)
        except (OSError, ValueError):
            return max(int(os.cpu_count() or 1), 1), True
        if quota is not None and period and period > 0:
            effective = quota / period
            return max(int(round(effective)), 1), False
        # 配额无限：回退 cpuset / 系统可见 CPU。
        cpuset = _count_cpuset_cpus(self._root, self._version)
        if cpuset is not None:
            return max(cpuset, 1), False
        return max(int(os.cpu_count() or 1), 1), True

    def _read_cpu_usage_usec(self) -> int | None:
        """cgroup CPU 累计使用量（微秒）。读取失败返回 None。"""
        if self._version == CGROUP_V2:
            try:
                raw = _read_line(self._root / "cpu.stat")
            except (OSError, ValueError):
                return None
            for line in raw.splitlines():
                if line.startswith("usage_usec"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
            return None
        # v1 cpuacct.usage 单位为纳秒。
        if self._version == CGROUP_V1:
            try:
                usage_ns = int(_read_line(self._root / "cpu" / "cpuacct.usage"))
            except (OSError, ValueError):
                return None
            return usage_ns // 1000
        return None

    def _cpu_utilization(self, effective_cpu_count: int) -> tuple[float, bool]:
        usage = self._read_cpu_usage_usec()
        now = self._monotonic()
        prev_usage = self._prev_usage_usec
        prev_now = self._prev_monotonic
        self._prev_usage_usec = usage
        self._prev_monotonic = now
        if usage is None or prev_usage is None or prev_now is None:
            # 首轮采样无基准或读取失败：暂无可用增量。
            return 0.0, usage is None
        delta_wall = now - prev_now
        if delta_wall <= 0:
            return 0.0, False
        delta_usage = max(usage - prev_usage, 0)
        utilization = (delta_usage / 1_000_000.0) / delta_wall / max(effective_cpu_count, 1) * 100.0
        return _clamp_percent(utilization), False

    # -- Memory ----------------------------------------------------------
    def _memory_available_mb(self) -> tuple[float, bool, bool]:
        """返回 (available_mb, degraded, failed)。failed=True 表示完全不可信。"""
        if self._version is None:
            return 0.0, False, True
        try:
            if self._version == CGROUP_V2:
                limit = _read_byte_value(self._root / "memory.max")
                current = _read_byte_value(self._root / "memory.current")
            else:
                limit = _read_byte_value(self._root / "memory" / "memory.limit_in_bytes")
                current = _read_byte_value(self._root / "memory" / "memory.usage_in_bytes")
        except (OSError, ValueError):
            return 0.0, False, True
        if limit is None or limit < 0:
            # 无明确 cgroup 内存限制：不能做 max - current 伪计算，标记 degraded。
            return 0.0, True, False
        return max(float(limit - current) / MEBIBYTE, 0.0), False, False


def _read_byte_value(path: Path) -> int | None:
    """读取 cgroup 字节型内存值。文件值为字符串 "max" 时返回 None（无限制）。"""
    raw = _read_line(path)
    if raw == "max":
        return None
    # v1 无限制常量为非常大的数字（如 9223372036854771712），按无限制处理。
    value = int(raw)
    if value > (1 << 62):
        return None
    return value


def build_resource_collector(
    config: ResourceConfig,
    *,
    psutil_module: Any | None = None,
) -> ResourceCollector:
    if config.mode == "cgroup":
        return CgroupResourceCollector(config, psutil_module=psutil_module)
    try:
        selected = psutil_module or importlib.import_module("psutil")
    except ModuleNotFoundError:
        if config.mode == "system":
            return NativeSystemResourceCollector()
        raise
    if config.mode == "process":
        return ProcessResourceCollector(config, selected)
    return SystemResourceCollector(selected)


def _native_available_memory_mb() -> float | None:
    """Return available memory in MiB, or None when no trusted measurement exists.

    语义：0.0 = 底层真的测到 0MiB（实际不可能）；None = 没有得到可信测量
    （API 调用失败）。调用方必须据此区分“真实低内存”和“遥测失败”。
    """
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        try:
            succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        if succeeded:
            return max(float(status.available_physical) / MEBIBYTE, 0.0)
        return None
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return max(float(pages * page_size) / MEBIBYTE, 0.0)


def _loaded_torch_gpu_available() -> bool:
    torch_module = sys.modules.get("torch")
    cuda = getattr(torch_module, "cuda", None)
    checker = getattr(cuda, "is_available", None)
    return bool(checker()) if callable(checker) else False


def _loaded_torch_npu_available() -> bool:
    torch_module = sys.modules.get("torch")
    npu = getattr(torch_module, "npu", None)
    checker = getattr(npu, "is_available", None)
    return bool(checker()) if callable(checker) else False


class AcceleratorDetector:
    def __init__(
        self,
        config: AcceleratorConfig,
        *,
        gpu_probe: Callable[[], bool] = _loaded_torch_gpu_available,
        npu_probe: Callable[[], bool] = _loaded_torch_npu_available,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.gpu_probe = gpu_probe
        self.npu_probe = npu_probe
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._snapshot: AcceleratorSnapshot | None = None
        self._last_gpu: bool | None = None
        self._last_npu: bool | None = None
        self._last_success_monotonic: float | None = None
        # EDGE-6: 每个设备独立的连续探测失败计数，用于节流 WARNING 刷屏。
        # 探测成功即清零；仅用于日志治理，不改变探测行为。
        self._probe_failure_streak: dict[str, int] = {}

    def detect(self) -> AcceleratorSnapshot:
        with self._lock:
            now = self.monotonic()
            # 仅缓存“成功探测”的结果；TTL 过期或上次探测异常时允许重新探测，
            # 避免“启动时 GPU=False，torch 就绪后仍永久 False”。
            if (
                self._snapshot is not None
                and self._snapshot.measurement_status == "OK"
                and self._probe_fresh(now)
            ):
                return self._snapshot
            self._snapshot = self._probe(now)
            return self._snapshot

    def _probe_fresh(self, now: float) -> bool:
        ttl = float(self.config.probe_ttl_seconds)
        if ttl <= 0:
            return False
        if self._last_success_monotonic is None:
            return False
        return now - self._last_success_monotonic < ttl

    def _probe(self, now: float) -> AcceleratorSnapshot:
        gpu_value, gpu_status = self._resolve(
            self.config.gpu_available_override,
            self.gpu_probe,
            self._last_gpu,
            "GPU",
        )
        npu_value, npu_status = self._resolve(
            self.config.npu_available_override,
            self.npu_probe,
            self._last_npu,
            "NPU",
        )
        if gpu_status == "OK":
            self._last_gpu = gpu_value
        if npu_status == "OK":
            self._last_npu = npu_value
        statuses = (gpu_status, npu_status)
        if "FAILED" in statuses:
            overall = "FAILED"
        elif "STALE" in statuses:
            overall = "STALE"
        else:
            overall = "OK"
            self._last_success_monotonic = now
        return AcceleratorSnapshot(
            gpu_available=gpu_value,
            npu_available=npu_value,
            measurement_status=overall,
        )

    def _resolve(
        self,
        override: bool | None,
        probe: Callable[[], bool],
        last_known: bool | None,
        name: str,
    ) -> tuple[bool, str]:
        if override is not None:
            # 显式覆盖视作一次成功解析：清零失败连续计数。
            self._probe_failure_streak[name] = 0
            return override, "OK"
        try:
            value = bool(probe())
            # EDGE-6: 探测成功，清零该设备的连续失败计数。
            self._probe_failure_streak[name] = 0
            return value, "OK"
        except Exception as exc:
            streak = self._probe_failure_streak.get(name, 0) + 1
            self._probe_failure_streak[name] = streak
            # EDGE-6: WARNING 节流 —— 第 1/3/10 次，此后每 30 次。
            # 探测失败不进入成功缓存，下一 interval 仍会重试；这里只治理日志，
            # 避免 interval=1s 时持续刷屏。
            if streak in (1, 3, 10) or streak % 30 == 0:
                self.logger.warning(
                    "%s 可用性检测失败: %s (连续失败第 %d 次)",
                    name,
                    type(exc).__name__,
                    streak,
                )
            if last_known is not None:
                # 探测异常不覆盖最近一次成功结果，仅标记降级。
                return last_known, "STALE"
            return False, "FAILED"
