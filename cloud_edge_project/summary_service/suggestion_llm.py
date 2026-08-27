from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass


MAX_SUGGESTION_CHARACTERS = 30
_CHINESE_CHARACTER = re.compile(r"[\u4e00-\u9fff]")
_NON_CHINESE_FORMATTING = re.compile(r"[A-Za-z`{}\[\]*#]")

SYSTEM_PROMPT = (
    "你是设备运维建议助手。请把结构化的最终维护动作翻译成一句简洁的中文建议。\n"
    "严格要求：\n"
    "1. 只输出一句中文建议，不超过30个字符。\n"
    "2. 以句号结尾，不输出分析、JSON或Markdown。\n"
    "3. 不猜测，不添加输入中没有的信息。"
)


@dataclass(frozen=True)
class SuggestionLlmResult:
    text: str
    success: bool
    latency_ms: float = 0.0
    fallback: bool = False


def normalize_suggestion(text: str, fallback_text: str) -> str:
    raw_text = str(text or "").strip()
    candidate = raw_text.splitlines()[0] if raw_text else ""
    if candidate and (
        _CHINESE_CHARACTER.search(candidate) is None
        or _NON_CHINESE_FORMATTING.search(candidate) is not None
    ):
        candidate = ""
    if not candidate:
        candidate = str(fallback_text).strip()
    if not candidate:
        candidate = "设备异常，请及时维护。"
    if not candidate.endswith("。"):
        candidate += "。"
    if len(candidate) > MAX_SUGGESTION_CHARACTERS:
        candidate = candidate[: MAX_SUGGESTION_CHARACTERS - 1].rstrip(
            "，、；：。!?！？ "
        ) + "。"
    return candidate


def build_suggestion_messages(
    *,
    device_id: str,
    final_action_grade: int,
    recommended_action: str,
    confidence: float,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"设备：{device_id}\n"
                f"最终动作等级：{final_action_grade}\n"
                f"最终维护动作：{recommended_action}\n"
                f"置信度：{confidence:.0%}\n"
            ),
        },
    ]


class SuggestionClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8005",
        timeout_seconds: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def translate(
        self,
        *,
        device_id: str,
        final_action_grade: int,
        recommended_action: str,
        confidence: float,
        fallback_text: str,
    ) -> SuggestionLlmResult:
        started = time.monotonic()
        payload = {
            "messages": build_suggestion_messages(
                device_id=device_id,
                final_action_grade=final_action_grade,
                recommended_action=recommended_action,
                confidence=confidence,
            ),
            "max_tokens": 32,
            "temperature": 0.1,
            "top_p": 0.9,
            "stop": ["\n"],
        }
        try:
            request = urllib.request.Request(
                self.base_url + "/v1/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            raw_text = body["choices"][0]["message"]["content"]
            if not str(raw_text).strip():
                raise ValueError("suggestion LLM returned empty text")
            return SuggestionLlmResult(
                text=normalize_suggestion(str(raw_text), fallback_text),
                success=True,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        except Exception:  # noqa: BLE001 - deterministic fallback is required.
            return SuggestionLlmResult(
                text=normalize_suggestion("", fallback_text),
                success=False,
                latency_ms=(time.monotonic() - started) * 1000.0,
                fallback=True,
            )
