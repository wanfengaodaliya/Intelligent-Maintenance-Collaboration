"""LLM-only suggestion layer for model updates.

Generates a human-readable "model update suggestion" (建议书) from the
structured update task. It never feeds back into data preparation or
training; the pipeline consumes only rule-decided structured fields.
"""

from __future__ import annotations

import logging
from typing import Any

from cloud_service.config import CloudSettings, load_cloud_settings
from cloud_service.errors import CloudServiceError
from cloud_service.llm_client import generate_text

LOGGER = logging.getLogger(__name__)

SUGGESTION_TIMEOUT_SECONDS = 30.0


def build_suggestion_prompt(task: dict[str, Any]) -> str:
    problem_type = task.get("problem_type") or "unknown"
    problem_context = task.get("problem_context") or {}
    evidence = task.get("evidence_snapshot") or {}
    model_type = task.get("model_type") or "distilled_h5"
    baseline = task.get("baseline_version") or "unknown"
    trainer_plan = task.get("trainer_plan") or {}
    return (
        "你是工业设备智能维护系统的模型更新顾问。请根据以下结构化信息，"
        "生成一份面向运维人员的「模型更新建议书」，用中文分点说明："
        "1) 检测到的问题与可能原因；2) 建议更新的模型与基线版本；"
        "3) 建议的训练数据与方式；4) 预期目标与注意事项。\n\n"
        f"问题类型：{problem_type}\n"
        f"问题上下文：{problem_context}\n"
        f"证据快照：{evidence}\n"
        f"模型类型：{model_type}\n"
        f"基线版本：{baseline}\n"
        f"训练计划：{trainer_plan}"
    )


def template_suggestion(task: dict[str, Any]) -> str:
    problem_type = task.get("problem_type") or "未知问题"
    evidence = task.get("evidence_snapshot") or {}
    sample_count = evidence.get("sample_count")
    model_type = task.get("model_type") or "distilled_h5"
    baseline = task.get("baseline_version") or "未知基线"
    count = f"（样本数 {sample_count}）" if isinstance(sample_count, int) else ""
    return (
        f"检测到 {problem_type} 问题{count}，建议基于 {baseline} 训练 {model_type} 模型。"
        "请按训练计划执行数据准备与离线训练，并在验证通过后提交人工审核。"
    )


def generate_suggestion(
    task: dict[str, Any],
    settings: CloudSettings | None = None,
) -> tuple[str, str]:
    """Return (text, source) where source is ``llm`` or ``template``."""
    try:
        settings = settings or load_cloud_settings()
        content = generate_text(
            [{"role": "user", "content": build_suggestion_prompt(task)}],
            settings,
            temperature=0.2,
            timeout=SUGGESTION_TIMEOUT_SECONDS,
        )
        return content, "llm"
    except Exception as exc:
        LOGGER.warning("model update suggestion LLM call failed: %s", exc)
        return template_suggestion(task), "template"
