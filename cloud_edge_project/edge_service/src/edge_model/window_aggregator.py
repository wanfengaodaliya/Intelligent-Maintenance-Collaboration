# -*- coding: utf-8 -*-
"""窗口聚合器：按 (task_id, sender_id) 双缓冲，按到达时刻切 1s 窗口。

逻辑已通过 tests/performance/closed_loop 验证（整数纳秒切窗，避免大单调基数
浮点漂移）。

正确性约束（P2）：
- 聚合器键 = (task_id, sender_id)：不同 task_id 的数据绝不进入同一窗口；
  任务结束/切换任务时必须先关闭旧任务的窗口（由 pipeline 负责调用 flush）；
- 两套时间严格分离：
    * 单调时钟（arrival / 窗口边界 window_start/end / close_ts）→ 调度、排队、超时；
    * 业务时间（包的真实 end_generate_timestamp_ns、feature_generated_at_ns）
      → 来自包本身或 time.time_ns()，绝不把单调时钟写进 Unix 时间字段；
- 特征聚合用字段映射（P2c）：映射未确认时只能用显式承认的占位均值。

其余：已关窗口后的迟到/乱序到达丢弃并计数；空窗口产出 is_empty=True；
稀疏窗口标记 WINDOW_SPARSE；窗口内记录每包身份（included_packets）。
"""
from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import WindowConfig
from .contracts import WindowAggregate

WINDOW_SPARSE_FLAG = "WINDOW_SPARSE"

# 支持的聚合方法
_AGG_METHODS = ("mean", "max", "min", "last", "first", "std")


def to_ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _walk_path(obj: Dict, path: List[str]) -> object:
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return None
        obj = obj[p]
    return obj


def _agg_values(values: List[float], method: str) -> float:
    if method == "mean":
        return round(sum(values) / len(values), 6)
    if method == "max":
        return round(max(values), 6)
    if method == "min":
        return round(min(values), 6)
    if method == "last":
        return round(values[-1], 6)
    if method == "first":
        return round(values[0], 6)
    if method == "std":
        return round(statistics.pstdev(values), 6)
    raise ValueError("未知聚合方法: %s（支持 %s）" % (method, _AGG_METHODS))


def merge_features(samples: List[dict], field_mapping: Optional[Dict[str, str]] = None) -> Dict:
    """按字段映射聚合特征。

    field_mapping 为空 → 占位均值（必须已由 config.placeholder_acknowledged 显式允许）。
    field_mapping 非空 → 按路径应用映射；未覆盖的数值叶字段抛错（不应默认取均值）。
    """
    if not samples:
        return {}
    template = samples[0].get("features", {})
    mapping = field_mapping or {}

    def rec(node: object, path: List[str]) -> object:
        if isinstance(node, dict):
            return {k: rec(v, path + [k]) for k, v in node.items()}
        if isinstance(node, (int, float)):
            key = ".".join(path)
            if not mapping:
                # 占位均值（P2c：仅技术闭环，须显式承认）
                vals = []
                for s in samples:
                    v = _walk_path(s.get("features", {}), path)
                    if isinstance(v, (int, float)) and v == v:
                        vals.append(float(v))
                return round(sum(vals) / len(vals), 6) if vals else None
            method = mapping.get(key)
            if method is None:
                raise ValueError("特征路径未在聚合映射中: %s（未确认字段不得默认取均值）" % key)
            vals = []
            for s in samples:
                v = _walk_path(s.get("features", {}), path)
                if isinstance(v, (int, float)) and v == v:
                    vals.append(float(v))
            if not vals:
                return None
            return _agg_values(vals, method)
        return node

    return rec(template, [])


def merge_quality(samples: List[dict]) -> Tuple[str, List[str]]:
    """窗口内质量：status 取最差（任一 warning 即 warning），flags 取并集。"""
    status = "good"
    flags: List[str] = []
    for s in samples:
        q = s.get("perception_quality") or {}
        if q.get("status") == "warning":
            status = "warning"
        for f in q.get("flags", []):
            if f not in flags:
                flags.append(f)
    return status, flags


def packet_identity(perception: dict) -> Dict[str, Any]:
    """从 PerceptionResult 提取包身份（含各自原始业务时间）。"""
    return {
        "task_id": perception.get("task_id"),
        "packet_id": perception.get("packet_id"),
        "sender_id": perception.get("sender_id"),
        "sequence_number": perception.get("sequence_number"),
        "end_generate_timestamp_ns": perception.get("end_generate_timestamp_ns"),
    }


@dataclass
class _WindowBuffer:
    wid: int
    start_ns: int   # 单调时钟边界（调度用）
    end_ns: int
    samples: List[Tuple[float, dict]] = field(default_factory=list)  # (arrival_ts_mono_sec, perception)


class WindowAggregator:
    """单 (task_id, sender_id) 的窗口聚合器。"""

    def __init__(self, task_id: str, sender_id: str, cfg: WindowConfig,
                 field_mapping: Optional[Dict[str, str]] = None,
                 clock=time.monotonic, wall_clock=time.time_ns):
        self.task_id = task_id
        self.sender_id = sender_id
        self.cfg = cfg
        self._field_mapping = field_mapping
        self._clock = clock                # 单调：到达/边界/排队/超时
        self._wall_clock = wall_clock      # 业务：feature_generated_at_ns 等 Unix 时间
        self._lock = threading.Lock()
        self._epoch_ns: Optional[int] = None
        self._active: Optional[_WindowBuffer] = None
        self.total_late_dropped = 0
        self.total_windows_closed = 0

    def _length_ns(self) -> int:
        return to_ns(self.cfg.length_seconds)

    def ingest(self, perception: dict, arrival_ts: Optional[float] = None) -> List[WindowAggregate]:
        """接收一条感知结果，返回因此关闭的窗口聚合列表（通常 0 或 1，间隔后可能多个）。"""
        if arrival_ts is None:
            arrival_ts = self._clock()
        arrival_ns = to_ns(arrival_ts)
        closed_buffers: List[_WindowBuffer] = []
        with self._lock:
            if self._epoch_ns is None:
                self._epoch_ns = arrival_ns
                length_ns = self._length_ns()
                self._active = _WindowBuffer(0, arrival_ns, arrival_ns + length_ns)
            if arrival_ns < self._active.start_ns:
                # 属于已关闭窗口的迟到/乱序数据：丢弃但计数
                self.total_late_dropped += 1
                return []
            while arrival_ns >= self._active.end_ns:
                closed_buffers.append(self._active)
                self._active = _WindowBuffer(
                    self._active.wid + 1, self._active.end_ns,
                    self._active.end_ns + self._length_ns(),
                )
            self._active.samples.append((arrival_ts, perception))
        out = [self._aggregate(b) for b in closed_buffers]
        self.total_windows_closed += len(out)
        return out

    def flush(self) -> Optional[WindowAggregate]:
        """关闭当前 active 窗口。flush 后视为一段流结束，再次 ingest 重新起算。"""
        with self._lock:
            if self._epoch_ns is None or self._active is None:
                return None
            buf = self._active
            self._active = None
            self._epoch_ns = None
        agg = self._aggregate(buf)
        self.total_windows_closed += 1
        return agg

    def _aggregate(self, buf: _WindowBuffer) -> WindowAggregate:
        cfg = self.cfg
        samples = buf.samples
        n = len(samples)
        is_empty = n == 0
        expected = cfg.expected_samples_per_window
        missing_ratio = round(max(0.0, 1.0 - n / expected), 6) if expected else 0.0
        sparse = (not is_empty) and n < cfg.min_samples_for_full

        q_status, q_flags = ("good", []) if is_empty else merge_quality([s for _, s in samples])
        if sparse:
            q_status = "warning"
            if WINDOW_SPARSE_FLAG not in q_flags:
                q_flags.append(WINDOW_SPARSE_FLAG)

        # 业务时间：来自包的真实 end_generate_timestamp_ns，不是单调到达时刻
        first_end_ns = samples[0][1].get("end_generate_timestamp_ns") if samples else None
        last_end_ns = samples[-1][1].get("end_generate_timestamp_ns") if samples else None

        payload: Dict = {}
        included: List[Dict[str, Any]] = []
        if not is_empty:
            base = samples[0][1]
            payload = dict(base)
            payload["features"] = merge_features([s for _, s in samples], self._field_mapping)
            payload["perception_quality"] = {"status": q_status, "flags": list(q_flags)}
            payload["sequence_number"] = base.get("sequence_number")
            payload["end_generate_timestamp_ns"] = last_end_ns          # 包的真实业务时间
            payload["feature_generated_at_ns"] = int(self._wall_clock())  # Unix 时间（非单调）
            for _, s in samples:
                included.append(packet_identity(s))

        return WindowAggregate(
            task_id=self.task_id,
            sender_id=self.sender_id,
            window_id=buf.wid,
            window_start_ns=buf.start_ns,      # 单调边界（内部调度）
            window_end_ns=buf.end_ns,          # 单调边界（内部调度）
            close_ts_ns=to_ns(self._clock()),  # 单调（内部）
            expected_samples=expected,
            sample_count=n,
            first_sample_ts_ns=first_end_ns,   # 业务时间
            last_sample_ts_ns=last_end_ns,     # 业务时间
            missing_ratio=missing_ratio,
            quality_status=q_status,
            quality_flags=q_flags,
            late_dropped_count=0,
            is_empty=is_empty,
            sparse=sparse,
            included_packets=included,
            payload=payload,
        )
