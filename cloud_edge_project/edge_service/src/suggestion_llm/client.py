# -*- coding: utf-8 -*-
"""建议 LLM HTTP 客户端（stdlib urllib，零额外依赖）。

调用本机 llama.cpp 服务（OpenAI 兼容 API），将结构化规则结果
翻译为一句通顺的中文维护建议。

服务地址默认为 http://127.0.0.1:8002，与边缘诊断模型（8001）不同端口。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass
class SuggestionLlmResult:
    """建议 LLM 调用结果。"""

    text: str
    success: bool
    latency_ms: float = 0.0
    fallback: bool = False


class SuggestionClient:
    """调用本机 llama.cpp 建议 LLM 的 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8002",
        timeout_seconds: float = 3.0,
        fallback_text: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.fallback_text = fallback_text or "设备异常，建议关注。"

    def suggest(self, messages: list[dict[str, str]]) -> SuggestionLlmResult:
        """调用 LLM 生成建议文本。

        参数：
            messages: OpenAI 兼容的 messages 列表

        返回：
            SuggestionLlmResult: 建议结果
        """
        t0 = time.monotonic()

        # 生成参数：短输出、低温度、遇到句号即停
        payload = {
            "messages": messages,
            "max_tokens": 32,
            "temperature": 0.1,
            "top_p": 0.9,
            "stop": ["。", "\n"],
        }

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.base_url + "/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"].strip()
            # 确保以句号结尾
            if not text.endswith("。"):
                text += "。"
            latency_ms = (time.monotonic() - t0) * 1000.0
            return SuggestionLlmResult(
                text=text, success=True, latency_ms=latency_ms
            )
        except Exception:  # noqa: BLE001
            latency_ms = (time.monotonic() - t0) * 1000.0
            return SuggestionLlmResult(
                text=self.fallback_text,
                success=False,
                latency_ms=latency_ms,
                fallback=True,
            )

    def health(self) -> bool:
        """检查建议 LLM 服务是否存活。"""
        try:
            req = urllib.request.Request(self.base_url + "/health")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("status") == "ok"
        except Exception:  # noqa: BLE001
            return False