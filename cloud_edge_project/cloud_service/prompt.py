"""Prompt and compact input construction for cloud model review."""

from __future__ import annotations

import json
import math
from typing import Any


CLOUD_SYSTEM_PROMPT = """
你是边缘—云协同智能维护系统中的云端复核模型。

你的任务是分析传感器统计数据和边缘模型结果，给出最终设备状态判断。
不得编造输入中不存在的测量数据；信息不足时采用保守判断。

只能返回一个合法 JSON 对象，不要输出 Markdown、代码围栏或额外说明：
{
  "label": "normal 或 abnormal",
  "confidence": 0.0,
  "risk_level": "low、medium 或 high",
  "action": "none、record_only、send_alert 或 stop_machine_check",
  "description": "简要说明判断依据"
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


def build_enhanced_analysis_messages(result: dict[str, Any]) -> list[dict[str, str]]:
    """Build a compact final-review prompt without raw waveform samples."""

    payload = {
        "preprocessing": {"input": result["input"], "data_quality": result["data_quality"], "limitations": result["limitations"]},
        "feature_extraction_and_operating_context": result.get("feature_context", {}),
        "enhanced_analysis": {"signal_evidence": result["signal_evidence"], "history_evidence": result["history_evidence"], "model_evidence": result["model_evidence"], "operating_conditions": result["operating_conditions"]},
    }
    return [{"role": "system", "content": CLOUD_SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
