# -*- coding: utf-8 -*-
"""Build the packet-level bearing diagnosis prompt."""
from __future__ import annotations

import json


PROMPT_VERSION = "edge-model-prompt/1.1"

SYSTEM_PROMPT = (
    "你是轴承设备状态诊断助手。把给定的完整感知结果转换为一个 JSON 诊断结论。\n"
    "诊断时必须综合检查所有特征组，不得只分析振动字段：\n"
    "- vibration：振动时域和频域全部特征；\n"
    "- phase_current_1、phase_current_2：两路电流全部特征；\n"
    "- current_relationship：电流关系特征；\n"
    "- operating_context：转速、负载扭矩、轴承径向载荷的全部统计值，以及轴承模块温度；\n"
    "- perception_quality：感知质量状态和标志。\n"
    "严格要求：\n"
    "1. 只输出一个 JSON 对象，禁止输出 <think>、解释、分析过程或 Markdown 代码块。\n"
    "2. JSON 只能包含以下三个字段，不允许增加或缺失：\n"
    "   edge_result: 只能是 \"normal\" | \"warning\" | \"fault\"\n"
    "   edge_risk_level: 只能是 \"low\" | \"medium\" | \"high\"\n"
    "   confidence: [0,1] 的浮点数，表示诊断分数\n"
    "3. 立即输出 JSON，不要有任何前言。"
)


def build_messages(model_input: dict) -> list[dict[str, str]]:
    """Build the common Transformers/vLLM messages for one packet."""

    body = json.dumps(model_input, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "完整感知结果：\n" + body + "\n\n请输出诊断 JSON。"},
    ]


def build_prompt(tokenizer, model_input: dict) -> str:
    """Build the tokenizer-specific prompt for one packet."""

    messages = build_messages(model_input)
    try:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    except Exception:  # noqa: BLE001 - a few tokenizers do not define a chat template
        return "\n\n".join(message["content"] for message in messages)
