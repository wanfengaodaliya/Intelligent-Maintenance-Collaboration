# -*- coding: utf-8 -*-
"""建议 LLM 模块：将结构化规则结果翻译为自然语言建议。"""

from .client import SuggestionClient, SuggestionLlmResult
from .prompt import build_suggestion_messages, SYSTEM_PROMPT, PROMPT_VERSION

__all__ = [
    "SuggestionClient",
    "SuggestionLlmResult",
    "build_suggestion_messages",
    "SYSTEM_PROMPT",
    "PROMPT_VERSION",
]