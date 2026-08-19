"""Prompt and compact input construction for cloud model review."""

from __future__ import annotations

import json
import math
from typing import Any


CLOUD_SYSTEM_PROMPT = """
你是边缘—云协同智能维护系统中的云端轴承单包复核模型。

你的输入属于一台设备的一次任务中的某个轴承和某个复核窗口。你需要根据云端重算特征、增强信号证据、边缘参考结果和历史基线，给出该轴承当前复核窗口的状态结论。
该结论不是整台设备的最终健康状态。不得替代轴承任务汇总、设备冲突检测或设备仲裁。不得仅根据单个轴承的单包结果直接宣布整台设备最终停机。
不得编造输入中不存在的测量数据；信息不足时采用保守判断。

只能返回一个合法 JSON 对象，不要输出 Markdown、代码围栏或额外说明：
{
  "label": "normal 或 fault",
  "confidence": 0.0,
  "risk_level": "low、medium 或 high",
  "recommended_action": "record_only、flag_for_task_aggregation 或 urgent_bearing_attention",
  "description": "当前轴承复核依据"
}
""".strip()


V01_CLOUD_SYSTEM_PROMPT = """
你是边缘—云协同智能维护系统的云端复核模型。根据任务传感器数据和边缘初判，给出保守、可追溯的单任务复核结论。
不得编造输入中不存在的测量数据；信息不足时采用保守判断。

只能返回一个合法 JSON 对象，不要输出 Markdown、代码围栏或额外说明：
{
  "label": "normal 或 fault",
  "confidence": 0.0,
  "risk_level": "low、medium 或 high",
  "recommended_action": "record_only、flag_for_task_aggregation 或 urgent_bearing_attention",
  "description": "复核依据"
}
""".strip()


def summarize_vibration(values: list[float]) -> dict[str, float | int]:
    """Return compact statistics for a validated vibration sequence."""

    count = len(values)
    if count == 0:
        raise ValueError("vibration must not be empty")
    mean = sum(values) / count
    return {
        "count": count,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(mean, 6),
        "rms": round(math.sqrt(sum(value * value for value in values) / count), 6),
        "peak_abs": round(max(abs(value) for value in values), 6),
    }


def build_cloud_messages(perception_result: dict[str, Any]) -> list[dict[str, str]]:
    """Build messages from structured perception data, not raw waveforms."""

    user_payload = {"perception_result": perception_result}
    return [
        {"role": "system", "content": CLOUD_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]


def build_v01_cloud_messages(request: dict[str, Any]) -> list[dict[str, str]]:
    """Build a compact V0.1 review prompt from the documented request fields."""

    return [
        {"role": "system", "content": V01_CLOUD_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]


def build_enhanced_analysis_messages(result: dict[str, Any]) -> list[dict[str, str]]:
    """Build a compact final-review prompt without raw waveform samples."""

    payload = {
        "preprocessing": {"input": result["input"], "data_quality": result["data_quality"], "limitations": result["limitations"]},
        "feature_extraction_and_operating_context": result.get("feature_context", {}),
        "enhanced_analysis": {"signal_evidence": result["signal_evidence"], "history_evidence": result["history_evidence"], "model_evidence": result["model_evidence"], "operating_conditions": result["operating_conditions"]},
    }
    return [{"role": "system", "content": CLOUD_SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
