# -*- coding: utf-8 -*-
"""共享基础设施：HTTP 客户端、容器内 DB 查询、统计工具。

本文件所有操作只读，不修改任何服务状态或数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import subprocess
import time
from pathlib import Path

import requests

# 服务端口（宿主机侧）
EDGE_01_URL = "http://127.0.0.1:8001"
EDGE_02_URL = "http://127.0.0.1:8002"
SCHEDULER_URL = "http://127.0.0.1:8003"
CLOUD_URL = "http://127.0.0.1:8004"
SUMMARY_URL = "http://127.0.0.1:8006"
NETWORK_URL = "http://127.0.0.1:8090"

EDGE_CONTAINERS = {"edge_01": EDGE_01_URL, "edge_02": EDGE_02_URL}

CLOUD_EDGE_ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT_ID = (
    os.getenv("DPB_EXPERIMENT_ID") or os.getenv("EXPERIMENT_ID") or ""
).strip()
EXPERIMENT_DATA_DIR = (
    Path(os.environ["DPB_EXPERIMENT_DATA_DIR"]).expanduser()
    if os.getenv("DPB_EXPERIMENT_DATA_DIR")
    else (
        CLOUD_EDGE_ROOT / "data" / "experiments" / EXPERIMENT_ID
        if EXPERIMENT_ID
        else None
    )
)

# Edge DB is inside each container. Cloud/Summary are host processes in the
# canonical start_project.ps1 flow and therefore use host SQLite paths.
EDGE_DB_PATH = (
    os.getenv("DPB_EDGE_DB_PATH")
    or (
        f"/app/data/experiments/{EXPERIMENT_ID}/edge_v12.db"
        if EXPERIMENT_ID
        else None
    )
)
CLOUD_DB = (
    Path(os.environ["DPB_CLOUD_DB"]).expanduser()
    if os.getenv("DPB_CLOUD_DB")
    else (EXPERIMENT_DATA_DIR / "cloud_review.db" if EXPERIMENT_DATA_DIR else None)
)
SUMMARY_DB = (
    Path(os.environ["DPB_SUMMARY_DB"]).expanduser()
    if os.getenv("DPB_SUMMARY_DB")
    else (EXPERIMENT_DATA_DIR / "summary_service.db" if EXPERIMENT_DATA_DIR else None)
)
CLOUD_CONTAINER = (os.getenv("DPB_CLOUD_CONTAINER") or "").strip() or None
CLOUD_CONTAINER_DB_PATH = os.getenv(
    "DPB_CLOUD_CONTAINER_DB_PATH", "/workspace/data/cloud_review.db"
)

HTTP_TIMEOUT = 10.0


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
    """GET JSON，失败抛出异常（工具应显式报错而不是吞掉）。"""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def percentile(sorted_values: list[float], p: float) -> float:
    """线性插值百分位（p 为 0-100）。"""
    if not sorted_values:
        return float("nan")
    values = sorted(sorted_values)
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * p / 100.0
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (rank - low)


def summarize(values: list[float]) -> dict:
    """均值/p50/p95/max/min 汇总。"""
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None, "min": None}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 2),
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
    }


def docker_exec_python(container: str, code: str, timeout: int = 60) -> str:
    """在容器内执行 python 代码（通过 stdin），返回 stdout。

    避免 PowerShell/docker exec 的引号问题：代码直接喂给 `python -`。
    """
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=code.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec {container} failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', errors='replace')[:800]}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def read_edge_db(container: str, sql: str, params: tuple = ()) -> list[tuple]:
    """在 Edge 容器内执行只读 SQL，返回行列表。"""
    if EDGE_DB_PATH is None:
        raise RuntimeError(
            "set DPB_EXPERIMENT_ID (or DPB_EDGE_DB_PATH) before reading Edge data"
        )
    code = (
        "import sqlite3, json, sys\n"
        f"conn = sqlite3.connect('file:{EDGE_DB_PATH}?mode=ro', uri=True)\n"
        f"rows = conn.execute({sql!r}, {params!r}).fetchall()\n"
        "print(json.dumps(rows, default=str, ensure_ascii=False))\n"
    )
    out = docker_exec_python(container, code)
    return json.loads(out.strip().splitlines()[-1])


def read_cloud_db(sql: str, params: tuple = ()) -> list[tuple]:
    """Read the run-scoped Cloud DB, on the host by default."""
    if CLOUD_CONTAINER:
        code = (
            "import sqlite3, json\n"
            f"conn = sqlite3.connect('file:{CLOUD_CONTAINER_DB_PATH}?mode=ro', uri=True)\n"
            f"rows = conn.execute({sql!r}, {params!r}).fetchall()\n"
            "print(json.dumps(rows, default=str, ensure_ascii=False))\n"
        )
        out = docker_exec_python(CLOUD_CONTAINER, code)
        return json.loads(out.strip().splitlines()[-1])
    if CLOUD_DB is None:
        raise RuntimeError(
            "set DPB_EXPERIMENT_ID (or DPB_CLOUD_DB) before reading Cloud data"
        )
    connection = sqlite3.connect(f"file:{CLOUD_DB}?mode=ro", uri=True)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def read_summary_db(sql: str, params: tuple = ()) -> list[tuple]:
    """宿主机 Summary DB 只读查询。"""
    if SUMMARY_DB is None:
        raise RuntimeError(
            "set DPB_EXPERIMENT_ID (or DPB_SUMMARY_DB) before reading Summary data"
        )
    conn = sqlite3.connect(f"file:{SUMMARY_DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def read_edge_memory_bytes(container: str) -> int:
    """读取容器 cgroup v2 memory.current（字节）。"""
    proc = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.current"],
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker exec {container} cat memory.current failed: {proc.stderr.decode(errors='replace')[:300]}")
    return int(proc.stdout.decode().strip())


def now_ns() -> int:
    return time.time_ns()


def resolve_window_index(
    device_id: str, task_id: str, bearing_id: str, sender_id: str,
    diagnosis_window_id: str, max_window: int = 80,
) -> int | None:
    """通过重算 diagnosis_window_id 反解窗口序号（1-based，单包 50ms 窗口）。

    Edge H5 是逐包推理：每包一个窗口，window_start == window_end == sequence。
    """
    import hashlib

    def _stable_id(prefix: str, identity: dict) -> str:
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    for seq in range(1, max_window + 1):
        candidate = _stable_id("dw_", {
            "device_id": device_id,
            "task_id": task_id,
            "bearing_id": bearing_id,
            "sender_id": sender_id,
            "window_start_sequence": seq,
            "window_end_sequence": seq,
        })
        if candidate == diagnosis_window_id:
            return seq
    return None
