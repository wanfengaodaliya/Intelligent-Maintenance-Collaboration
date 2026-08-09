"""Decision gate based on already-computed global-analysis metrics."""

from __future__ import annotations

from typing import Any


def decide_update(global_analysis: dict[str, Any]) -> str:
    """Return the only supported first-phase update decision."""

    reviewed_count = global_analysis.get("reviewed_packet_count")
    correction_rate = global_analysis.get("cloud_correction_rate")
    if not isinstance(reviewed_count, int) or reviewed_count < 20:
        return "observe"
    if not isinstance(correction_rate, (int, float)) or correction_rate < 0.15:
        return "observe"
    return "create_update"
