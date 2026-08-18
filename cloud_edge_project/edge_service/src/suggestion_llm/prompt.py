# -*- coding: utf-8 -*-
"""建议 LLM 提示词模板：将结构化规则结果翻译为一句通顺的中文维护建议。"""
from __future__ import annotations

PROMPT_VERSION = "edge-suggestion-prompt/1.0"

SYSTEM_PROMPT = (
    "你是设备运维建议助手。你的任务是将结构化的诊断结果和规则分析结果，"
    "转换为一句通顺、简洁的中文维护建议。\n"
    "严格要求：\n"
    "1. 只输出一句中文建议，不超过 30 个字，不要多余的解释、分析或标点。\n"
    "2. 输出的建议必须是完整的一句话，以句号结尾。\n"
    "3. 不要输出 JSON、Markdown 或任何非中文内容。\n"
    "4. 不要猜测、不要添加输入中没有的信息。\n"
    "5. 如果建议类型是 NO_ACTION，输出"设备运行正常，无需操作。""
)


def build_suggestion_messages(
    device_id: str,
    label: str,
    confidence: float,
    risk_level: str,
    suggestion_type: str,
    trend: str = "",
) -> list[dict[str, str]]:
    """构建建议 LLM 的 messages 输入。

    参数：
        device_id: 设备 ID
        label: 诊断结果 (normal/warning/fault)
        confidence: 置信度 (0~1)
        risk_level: 风险等级 (low/medium/high)
        suggestion_type: 规则引擎决定的建议类型
        trend: 趋势描述（可选）

    返回：
        OpenAI 兼容的 messages 列表
    """
    user_content = (
        f"设备：{device_id}\n"
        f"状态：{label}\n"
        f"置信度：{confidence:.0%}\n"
        f"风险等级：{risk_level}\n"
        f"建议类型：{suggestion_type}\n"
    )
    if trend:
        user_content += f"趋势：{trend}\n"

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]