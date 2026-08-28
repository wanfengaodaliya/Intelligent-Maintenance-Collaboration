from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import stable_id
from .suggestion_llm import SuggestionClient, SuggestionLlmResult, normalize_suggestion


FALLBACK_BY_ACTION = {
    "continue_operation": "设备运行正常，继续运行。",
    "enhanced_monitoring": "设备状态轻微异常，请加强监测。",
    "scheduled_inspection": "设备存在异常，请安排检查。",
    "shutdown": "设备故障风险高，请立即停机。",
}


def build_final_suggestion(
    source: Mapping[str, Any],
    *,
    client: SuggestionClient | None = None,
    fallback_override: str = "",
) -> dict[str, Any]:
    recommended_action = str(source["recommended_action"])
    final_action_level = int(source["final_action_level"])

    fallback_text = fallback_override.strip() or FALLBACK_BY_ACTION[recommended_action]
    if client is None:
        translated = SuggestionLlmResult(
            text=normalize_suggestion("", fallback_text),
            success=False,
            fallback=True,
        )
    else:
        translated = client.translate(
            device_id=str(source["device_id"]),
            final_action_level=final_action_level,
            recommended_action=recommended_action,
            confidence=float(source["confidence"]),
            fallback_text=fallback_text,
        )

    summary_result_id = str(source["summary_result_id"])
    return {
        "result_id": stable_id("suggestion", summary_result_id),
        "summary_result_id": summary_result_id,
        "device_id": str(source["device_id"]),
        "window_start_sequence": int(source["window_start_sequence"]),
        "window_end_sequence": int(source["window_end_sequence"]),
        "result_status": "FINAL",
        "final_action_level": final_action_level,
        "recommended_action": recommended_action,
        "confidence": float(source["confidence"]),
        "suggestion": translated.text,
        "suggestion_type": recommended_action,
        "priority": (
            "high"
            if final_action_level >= 3
            else "medium"
            if final_action_level == 2
            else "low"
        ),
        "generated_by": "llm" if translated.success else "rule",
        "created_at_ns": int(source["closed_at_ns"]),
    }
