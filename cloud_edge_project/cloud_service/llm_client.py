"""Standalone LLM client for cloud-side explanation generation.

Independent of the cloud inference backend (CLOUD_BACKEND). Used by the
model-update suggestion layer to explain update decisions through an
OpenAI-compatible endpoint (vLLM / llama.cpp).
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

import requests

from cloud_service.config import CloudSettings
from cloud_service.errors import CloudServiceError


def _headers(settings: CloudSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.vllm_api_key:
        headers["Authorization"] = f"Bearer {settings.vllm_api_key}"
    return headers


def generate_text(
    messages: list[dict[str, str]],
    settings: CloudSettings,
    *,
    max_tokens: int = 512,
    temperature: float = 0.1,
    timeout: float | None = None,
) -> str:
    """Call the OpenAI-compatible LLM endpoint and return the text content."""

    start = perf_counter()
    try:
        response = requests.post(
            settings.vllm_url,
            headers=_headers(settings),
            json={
                "model": settings.vllm_model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=timeout if timeout is not None else settings.vllm_timeout_seconds,
        )
        response.raise_for_status()
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise CloudServiceError(
            "CLOUD_UNAVAILABLE",
            "LLM service is unavailable",
            503,
        ) from exc
    except requests.HTTPError as exc:
        raise CloudServiceError(
            "CLOUD_UNAVAILABLE",
            "LLM service returned an error",
            503,
        ) from exc

    try:
        response_payload = response.json()
        content = response_payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise CloudServiceError(
            "MODEL_INFER_FAILED",
            "LLM returned an invalid response",
            502,
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise CloudServiceError(
            "MODEL_INFER_FAILED",
            "LLM response content is empty",
            502,
        )
    return content.strip()
