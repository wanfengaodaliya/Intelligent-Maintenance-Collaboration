"""跨场景复用的全局分析统计函数。"""

from __future__ import annotations

from typing import Any


DEFAULT_TASK_LIMIT = 20
MIN_TASK_COUNT = 5
TREND_THRESHOLD = 0.30
_STATE_SCORES = {"normal": 0, "warning": 1, "abnormal": 2}


def analyze_state_trend(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    """统计有效任务的状态占比并比较前后两个时间窗口。"""

    valid_rows = [row for row in task_results if row.get("state") in _STATE_SCORES]
    valid_rows.sort(key=lambda row: row.get("completed_at_ns", 0))
    count = len(valid_rows)
    counts = {state: sum(row["state"] == state for row in valid_rows) for state in _STATE_SCORES}
    rates = {
        f"{state}_rate": counts[state] / count if count else 0.0
        for state in _STATE_SCORES
    }
    latest_state = valid_rows[-1]["state"] if valid_rows else None
    result: dict[str, Any] = {
        "valid_task_count": count,
        "latest_state": latest_state,
        "normal_count": counts["normal"],
        "warning_count": counts["warning"],
        "abnormal_count": counts["abnormal"],
        **rates,
    }
    if count < MIN_TASK_COUNT:
        return {**result, "trend": "insufficient_data", "trend_delta": None}

    midpoint = count // 2
    older_scores = [_STATE_SCORES[row["state"]] for row in valid_rows[:midpoint]]
    recent_scores = [_STATE_SCORES[row["state"]] for row in valid_rows[midpoint:]]
    delta = sum(recent_scores) / len(recent_scores) - sum(older_scores) / len(older_scores)
    trend = "stable"
    if delta >= TREND_THRESHOLD:
        trend = "degrading"
    elif delta <= -TREND_THRESHOLD:
        trend = "improving"
    return {**result, "trend": trend, "trend_delta": delta}


def analyze_edge_cloud_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """统计已完成云端复核样本中的边云一致与修正比例。"""

    reviewed_rows = [
        row
        for row in rows
        if isinstance(row.get("edge_label"), str) and isinstance(row.get("cloud_label"), str)
    ]
    count = len(reviewed_rows)
    agreement_count = sum(row["edge_label"] == row["cloud_label"] for row in reviewed_rows)
    return {
        "reviewed_packet_count": count,
        "edge_cloud_agreement_rate": agreement_count / count if count else None,
        "cloud_correction_rate": (count - agreement_count) / count if count else None,
        "note": "该指标仅统计被调度到云端复核的样本，不代表全部边缘样本准确率",
    }


def analyze_conflict_rate(task_results: list[dict[str, Any]]) -> dict[str, Any]:
    """计算任务级冲突比例。"""

    count = len(task_results)
    conflict_count = sum(bool(row.get("has_conflict")) for row in task_results)
    return {
        "conflict_count": conflict_count,
        "conflict_rate": conflict_count / count if count else 0.0,
    }


def analyze_arbitration_success(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按仲裁记录的 resolved 状态计算成功比例。"""

    count = len(rows)
    resolved_count = sum(row.get("status") == "resolved" for row in rows)
    return {
        "arbitration_count": count,
        "arbitration_success_rate": resolved_count / count if count else None,
    }
