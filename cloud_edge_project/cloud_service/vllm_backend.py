"""OpenAI-compatible vLLM backend for real cloud inference."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

import requests

from cloud_service.config import CloudSettings
from cloud_service.errors import CloudServiceError
from cloud_service.prompt import (
    build_cloud_messages,
    build_v01_cloud_messages,
)


CLOUD_NODE_ID = "cloud_1"
ALLOWED_LABELS = {"normal", "fault"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_RECOMMENDED_ACTIONS = {
    "record_only",
    "flag_for_task_aggregation",
    "urgent_bearing_attention",
}
_LEGACY_ACTION_TO_RECOMMENDATION = {
    "none": "record_only",
    "record_only": "record_only",
    "send_alert": "flag_for_task_aggregation",
    "stop_machine_check": "urgent_bearing_attention",
}


def _headers(settings: CloudSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.vllm_api_key:
        headers["Authorization"] = f"Bearer {settings.vllm_api_key}"
    return headers


def _model_result(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("model response content is empty")
    stripped = content.strip()
    if stripped.startswith("```"):
        raise ValueError("model response must be plain JSON without code fences")
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")

    label = parsed.get("label")
    if label not in ALLOWED_LABELS:
        raise ValueError("model label must be normal or fault")

    confidence = parsed.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= float(confidence) <= 1
    ):
        raise ValueError("model confidence must be between 0 and 1")

    risk_level = parsed.get("risk_level")
    if risk_level not in ALLOWED_RISK_LEVELS:
        raise ValueError("model risk_level is invalid")

    recommended_action = parsed.get("recommended_action")
    if recommended_action is None:
        recommended_action = _LEGACY_ACTION_TO_RECOMMENDATION.get(parsed.get("action"))
    if recommended_action not in ALLOWED_RECOMMENDED_ACTIONS:
        raise ValueError("model recommended_action is invalid")

    description = parsed.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("model description must be a non-empty string")

    return {
        "label": label,
        "confidence": round(float(confidence), 4),
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "description": description.strip(),
    }


def _request_structured_result(
    messages: list[dict[str, str]], settings: CloudSettings
) -> tuple[dict[str, Any], float]:
    start = perf_counter()
    try:
        response = requests.post(
            settings.vllm_url,
            headers=_headers(settings),
            json={
                "model": settings.vllm_model_name,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 512,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            },
            timeout=settings.vllm_timeout_seconds,
        )
        response.raise_for_status()
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise CloudServiceError(
            "CLOUD_UNAVAILABLE",
            "vLLM service is unavailable",
            503,
        ) from exc
    except requests.HTTPError as exc:
        raise CloudServiceError(
            "CLOUD_UNAVAILABLE",
            "vLLM service returned an error",
            503,
        ) from exc

    try:
        response_payload = response.json()
        content = response_payload["choices"][0]["message"]["content"]
        model_result = _model_result(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise CloudServiceError(
            "MODEL_INFER_FAILED",
            "vLLM returned an invalid model response",
            502,
        ) from exc

    return model_result, (perf_counter() - start) * 1000


def infer_vllm(
    perception_result: dict[str, Any],
    settings: CloudSettings,
) -> dict[str, Any]:
    """Call vLLM and convert its answer to the project CloudResult."""

    model_result, elapsed_ms = _request_structured_result(
        build_cloud_messages(perception_result), settings
    )
    return {
        "analysis_scope": "bearing_packet_review",
        "device_id": perception_result["device_id"],
        "task_id": perception_result["task_id"],
        "bearing_id": perception_result["bearing_id"],
        "packet_id": perception_result["packet_id"],
        "sender_id": perception_result["sender_id"],
        "cloud_node_id": CLOUD_NODE_ID,
        "model_name": settings.vllm_model_name,
        "label": model_result["label"],
        "confidence": model_result["confidence"],
        "risk_level": model_result["risk_level"],
        "cloud_latency_ms": round(elapsed_ms, 2),
        "decision": {
            "recommended_action": model_result["recommended_action"],
            "action": model_result["recommended_action"],
            "description": model_result["description"],
        },
    }


def infer_v01_vllm(request: dict[str, Any], settings: CloudSettings) -> dict[str, Any]:
    """Call vLLM for a documented V0.1 CloudRequest."""

    model_result, elapsed_ms = _request_structured_result(
        build_v01_cloud_messages(request), settings
    )
    action = (
        "ignore"
        if model_result["recommended_action"] == "record_only"
        else "send_alert"
    )
    return {
        "task_id": request["task_id"],
        "node_id": CLOUD_NODE_ID,
        "model_name": settings.vllm_model_name,
        "label": model_result["label"],
        "confidence": model_result["confidence"],
        "risk_level": model_result["risk_level"],
        "cloud_latency_ms": round(elapsed_ms, 2),
        "decision": {
            "action": action,
            "description": model_result["description"],
        },
    }
