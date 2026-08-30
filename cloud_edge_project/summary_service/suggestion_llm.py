"""Compatibility exports for bearing maintenance suggestions."""

from scenarios.bearing.summary_service.suggestion_llm import (
    MAX_SUGGESTION_CHARACTERS,
    SYSTEM_PROMPT,
    SuggestionClient,
    SuggestionLlmResult,
    build_suggestion_messages,
    normalize_suggestion,
)

__all__ = [
    "MAX_SUGGESTION_CHARACTERS",
    "SYSTEM_PROMPT",
    "SuggestionClient",
    "SuggestionLlmResult",
    "build_suggestion_messages",
    "normalize_suggestion",
]
