"""轴承场景专用的长期风险分析。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cloud_service.global_analysis.common_analyzer import analyze_state_trend
from scenarios.bearing.cloud.global_analysis.config import (
    BEARING_ABNORMAL_RATE_THRESHOLD,
)


def analyze_bearing_risk(
    bearing_task_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """按轴承聚合风险，并以异常率和预警率确定主要风险轴承。"""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bearing_task_results:
        bearing_id = row.get("bearing_id")
        if isinstance(bearing_id, str) and bearing_id:
            grouped[bearing_id].append(row)

    bearings: list[dict[str, Any]] = []
    for bearing_id in sorted(grouped):
        trend = analyze_state_trend(
            [
                {
                    "state": row.get("bearing_state"),
                    "completed_at_ns": row.get("completed_at_ns", 0),
                }
                for row in grouped[bearing_id]
            ]
        )
        bearings.append(
            {
                "bearing_id": bearing_id,
                "task_count": trend["valid_task_count"],
                "normal_count": trend["normal_count"],
                "warning_count": trend["warning_count"],
                "abnormal_count": trend["abnormal_count"],
                "warning_rate": trend["warning_rate"],
                "abnormal_rate": trend["abnormal_rate"],
                "trend": trend["trend"],
            }
        )
    primary = max(
        bearings,
        key=lambda item: (item["abnormal_rate"], item["warning_rate"], item["bearing_id"]),
        default=None,
    )
    return {
        "primary_risk_bearing_id": primary["bearing_id"] if primary else None,
        "bearings": bearings,
    }


def build_bearing_maintenance_recommendations(
    state_trend: dict[str, Any], bearing_risk: dict[str, Any]
) -> list[str]:
    """依据轴承场景指标生成维护建议。"""

    recommendations: list[str] = []
    if state_trend["trend"] == "degrading":
        recommendations.append("设备近期健康状态呈恶化趋势，建议重点关注。")
    primary_id = bearing_risk.get("primary_risk_bearing_id")
    primary = next(
        (
            item
            for item in bearing_risk.get("bearings", [])
            if item["bearing_id"] == primary_id
        ),
        None,
    )
    if primary and primary["abnormal_rate"] >= BEARING_ABNORMAL_RATE_THRESHOLD:
        recommendations.append(
            f"{primary_id} 异常任务占比较高，建议优先检查该轴承。"
        )
    return recommendations
