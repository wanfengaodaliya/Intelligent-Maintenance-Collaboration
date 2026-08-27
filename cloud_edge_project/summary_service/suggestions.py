from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import ACTION_BY_GRADE, stable_id
from .suggestion_llm import SuggestionClient, SuggestionLlmResult, normalize_suggestion


GRADE_BY_ACTION = {action: grade for grade, action in ACTION_BY_GRADE.items()}

FALLBACK_BY_ACTION = {
    "continue_operation": "设备运行正常，继续运行。",
    "enhanced_monitoring": "设备状态轻微异常，请加强监测。",
    "scheduled_inspection": "设备存在异常，请安排检查。",
    "urgent_intervention": "设备风险较高，请尽快干预。",
    "shutdown": "设备故障风险高，请立即停机。",
}


def action_grade_for(action: str) -> int:
    try:
        return GRADE_BY_ACTION[str(action)]
    except KeyError:
        raise ValueError(f"unsupported final maintenance action: {action}") from None


def build_final_suggestion(
    source: Mapping[str, Any],
    *,
    client: SuggestionClient | None = None,
    fallback_override: str = "",
) -> dict[str, Any]:
    recommended_action = str(source["recommended_action"])
    final_action_grade = int(source["final_action_grade"])
    if ACTION_BY_GRADE.get(final_action_grade) != recommended_action:
        raise ValueError("recommended_action does not match final_action_grade")

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
            final_action_grade=final_action_grade,
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
        "final_action_grade": final_action_grade,
        "recommended_action": recommended_action,
        "confidence": float(source["confidence"]),
        "suggestion": translated.text,
        "suggestion_type": recommended_action,
        "priority": (
            "high"
            if final_action_grade >= 3
            else "medium"
            if final_action_grade == 2
            else "low"
        ),
        "generated_by": "llm" if translated.success else "rule",
        "created_at_ns": int(source["closed_at_ns"]),
    }
