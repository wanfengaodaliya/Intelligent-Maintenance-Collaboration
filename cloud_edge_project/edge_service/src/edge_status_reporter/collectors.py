# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import ctypes
import logging
import os
import sys
import threading
from typing import Any, Callable, Protocol

from .config import AcceleratorConfig, ResourceConfig
from .contracts import AcceleratorSnapshot, ResourceSnapshot


MEBIBYTE = 1024 * 1024


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
        return ResourceSnapshot(
            logical_cpu_count=max(int(os.cpu_count() or 1), 1),
            cpu_utilization_percent=0.0,
            memory_available_mb=_native_available_memory_mb(),
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


def build_resource_collector(
    config: ResourceConfig,
    *,
    psutil_module: Any | None = None,
) -> ResourceCollector:
    try:
        selected = psutil_module or importlib.import_module("psutil")
    except ModuleNotFoundError:
        if config.mode == "system":
            return NativeSystemResourceCollector()
        raise
    if config.mode == "process":
        return ProcessResourceCollector(config, selected)
    return SystemResourceCollector(selected)


def _native_available_memory_mb() -> float:
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
            return 0.0
        if succeeded:
            return max(float(status.available_physical) / MEBIBYTE, 0.0)
        return 0.0
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0.0
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
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.gpu_probe = gpu_probe
        self.npu_probe = npu_probe
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._snapshot: AcceleratorSnapshot | None = None

    def detect(self) -> AcceleratorSnapshot:
        with self._lock:
            if self._snapshot is None:
                self._snapshot = AcceleratorSnapshot(
                    gpu_available=self._resolve(
                        self.config.gpu_available_override,
                        self.gpu_probe,
                        "GPU",
                    ),
                    npu_available=self._resolve(
                        self.config.npu_available_override,
                        self.npu_probe,
                        "NPU",
                    ),
                )
            return self._snapshot

    def _resolve(
        self,
        override: bool | None,
        probe: Callable[[], bool],
        name: str,
    ) -> bool:
        if override is not None:
            return override
        try:
            return bool(probe())
        except Exception as exc:
            self.logger.warning("%s 可用性检测失败: %s", name, type(exc).__name__)
            return False
