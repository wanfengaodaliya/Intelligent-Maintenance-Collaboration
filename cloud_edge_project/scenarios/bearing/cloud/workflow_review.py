"""Temporary deterministic processors behind the workflow review job facade."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from core.bearing_actions import ACTION_TO_STATE, action_for_grade


def review_packet(request: dict[str, Any]) -> dict[str, Any]:
    packet_result = request["packet_result"]
    raw_packet = request["raw_packet"]
    values = raw_packet["data"]["vibration"]["values"]
    if not isinstance(values, list) or not values:
        raise ValueError("INVALID_RAW_PACKET")
    average = sum(float(item) for item in values) / len(values)
    rms = math.sqrt(sum((float(item) - average) ** 2 for item in values) / len(values))
    if rms >= 3.0:
        grade = 3
        risk = "high"
    elif rms >= 1.0:
        grade = 2
        risk = "medium"
    else:
        grade = 0
        risk = "low"
    return {
        **packet_result,
        "result_id": packet_result["result_id"] + "_cloud",
        "action_grade": grade,
        "recommended_action": action_for_grade(grade),
        "confidence": 0.92,
        "risk_level": risk,
        "decision_source": "FINAL_CLOUD",
        "cloud_model_version": "cloud_packet_mock_v1",
    }


def review_window(request: dict[str, Any], raw_packets: list[dict[str, Any]]) -> dict[str, Any]:
    if len(raw_packets) != 20:
        raise ValueError("WINDOW_REQUIRES_20_RAW_PACKETS")
    window = request["window_result"]
    packet_grades = request.get("packet_action_grades")
    if isinstance(packet_grades, list) and len(packet_grades) == 20:
        counts = Counter(int(item) for item in packet_grades)
        grade = max(counts, key=lambda item: (counts[item], item))
    else:
        grade = int(window["action_grade"])
    return {
        **window,
        "result_id": window["result_id"] + "_cloud",
        "action_grade": grade,
        "recommended_action": action_for_grade(grade),
        "confidence": max(float(window["confidence"]), 0.90),
        "result_source": "FINAL_CLOUD",
        "review_status": "SUCCEEDED",
        "review_required": False,
        "review_reasons": [],
        "cloud_model_version": "cloud_bearing_window_mock_v1",
        "reviewed_raw_packet_count": len(raw_packets),
    }


def device_arbitration_request(device: dict[str, Any], review_id: str) -> dict[str, Any]:
    bearings = []
    for item in device["bearing_results"]:
        grade = int(item["recommended_action_grade"])
        bearings.append({
            "bearing_id": item["bearing_id"],
            "sender_id": item["sender_id"],
            "bearing_state": ACTION_TO_STATE[action_for_grade(grade)],
            "confidence": item["confidence"],
            "data_quality_score": item["data_quality_score"],
            "risk_level": "high" if grade >= 3 else "medium" if grade == 2 else "low",
            "recommended_action": action_for_grade(grade),
            "rule_facts": item.get("rule_facts", []),
            "summary": item,
        })
    return {
        "scenario_type": "bearing",
        "conflict_id": review_id,
        "subject_id": device["device_id"],
        "task_id": device["task_id"],
        "scenario_payload": {"bearing_results": bearings},
    }


def reviewed_device_result(device: dict[str, Any], arbitration: dict[str, Any]) -> dict[str, Any]:
    from core.bearing_actions import grade_for_action

    final_action = arbitration.get("final_action")
    if final_action is None:
        grade = max(
            int(item["recommended_action_grade"])
            for item in device["bearing_results"]
        )
        final_action = action_for_grade(grade)
        final_state = ACTION_TO_STATE[final_action]
        confidence = max(float(item["confidence"]) for item in device["bearing_results"])
    else:
        grade = grade_for_action(final_action)
        final_state = arbitration["final_state"]
        confidence = arbitration["confidence"]
    return {
        **device,
        "status": "FINAL",
        "action_grade": grade,
        "recommended_action": final_action,
        "conflict": True,
        "decision_source": "CLOUD",
        "final_report": {
            "report_status": "STRUCTURED_RESULT_READY_FOR_LLM",
            "final_state": final_state,
            "final_action": final_action,
            "confidence": confidence,
            "arbitration_id": arbitration["arbitration_id"],
        },
    }
