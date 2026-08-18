# -*- coding: utf-8 -*-
"""建议规则引擎：根据当前诊断结果和历史趋势，生成结构化建议。

规则引擎只负责决策（建议类型、优先级），不生成自然语言。
自然语言由 suggestion_llm.client 根据规则结果翻译。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SuggestionResult:
    """规则引擎输出的结构化建议，供 LLM 翻译为自然语言。"""

    suggestion_type: str       # URGENT_MAINTENANCE / SCHEDULE_MAINTENANCE / MONITOR / NO_ACTION
    priority: str              # high / medium / low
    reason: str                # 给 LLM 填充用的结构化原因
    trend: str = ""            # 趋势描述，如"异常率从10%升至70%"
    maintenance_window: Optional[str] = None  # "immediate" / "24h" / "7d" / None


def _recent_anomaly_rate(history: list[dict], window: int = 10) -> float:
    """计算最近 N 条记录中异常（warning / fault）的比例。"""
    if not history:
        return 0.0
    recent = history[-window:]
    anomaly_count = sum(
        1 for h in recent if h.get("edge_result") in ("warning", "fault")
    )
    return anomaly_count / len(recent)


def _trend_description(history: list[dict], window: int = 10) -> str:
    """生成趋势描述：将历史窗口分段，比较前后半段异常率。"""
    if not history:
        return ""
    recent = history[-window:]
    if len(recent) < 4:
        return ""
    mid = len(recent) // 2
    first_half = recent[:mid]
    second_half = recent[mid:]
    first_rate = _recent_anomaly_rate(first_half, len(first_half))
    second_rate = _recent_anomaly_rate(second_half, len(second_half))
    if second_rate > first_rate + 0.2:
        return f"异常率从{first_rate:.0%}上升至{second_rate:.0%}"
    elif second_rate < first_rate - 0.2:
        return f"异常率从{first_rate:.0%}下降至{second_rate:.0%}"
    return ""


def evaluate_suggestion(
    device_id: str,
    current_label: str,      # normal / warning / fault
    confidence: float,
    risk_level: str,         # low / medium / high
    history: list[dict],     # 最近 N 包历史结果，每包含 edge_result、confidence、risk_level
) -> SuggestionResult:
    """规则引擎主入口：根据当前结果和历史趋势，决定建议类型。

    参数：
        device_id: 设备 ID
        current_label: 当前诊断结果 (normal/warning/fault)
        confidence: 当前置信度 (0~1)
        risk_level: 当前风险等级 (low/medium/high)
        history: 历史结果列表，按时间顺序，每项包含 edge_result/confidence/risk_level

    返回：
        SuggestionResult: 结构化建议
    """
    anomaly_rate = _recent_anomaly_rate(history)
    trend = _trend_description(history)

    # 规则 1：故障 + 高置信度或高风险 → 立即停机
    if current_label == "fault" and (confidence >= 0.8 or risk_level == "high"):
        return SuggestionResult(
            suggestion_type="URGENT_MAINTENANCE",
            priority="high",
            reason=f"设备 {device_id} 处于故障状态（置信度 {confidence:.0%}，风险 {risk_level}）",
            trend=trend,
            maintenance_window="immediate",
        )

    # 规则 2：故障但置信度较低 → 交叉验证后安排检修
    if current_label == "fault":
        return SuggestionResult(
            suggestion_type="SCHEDULE_MAINTENANCE",
            priority="high",
            reason=f"设备 {device_id} 检测到故障（置信度 {confidence:.0%}），建议与云端结果交叉验证",
            trend=trend,
            maintenance_window="24h",
        )

    # 规则 3：警告 + 历史异常率高于 50% → 安排检修
    if current_label == "warning" and anomaly_rate >= 0.5:
        return SuggestionResult(
            suggestion_type="SCHEDULE_MAINTENANCE",
            priority="medium",
            reason=f"设备 {device_id} 持续异常（近10包异常率 {anomaly_rate:.0%}）",
            trend=trend,
            maintenance_window="24h",
        )

    # 规则 4：警告 + 上升趋势 → 建议关注
    if current_label == "warning" and "上升" in trend:
        return SuggestionResult(
            suggestion_type="SCHEDULE_MAINTENANCE",
            priority="medium",
            reason=f"设备 {device_id} 异常率呈上升趋势",
            trend=trend,
            maintenance_window="7d",
        )

    # 规则 5：警告 → 持续观察
    if current_label == "warning":
        return SuggestionResult(
            suggestion_type="MONITOR",
            priority="low",
            reason=f"设备 {device_id} 出现轻微异常（置信度 {confidence:.0%}）",
            trend=trend,
        )

    # 规则 6：正常 + 历史异常率 > 0 → 恢复通知
    if current_label == "normal" and anomaly_rate > 0:
        return SuggestionResult(
            suggestion_type="MONITOR",
            priority="low",
            reason=f"设备 {device_id} 已恢复正常",
            trend=trend,
        )

    # 规则 7：持续正常 → 无需操作
    return SuggestionResult(
        suggestion_type="NO_ACTION",
        priority="low",
        reason=f"设备 {device_id} 状态正常",
        trend=trend,
    )