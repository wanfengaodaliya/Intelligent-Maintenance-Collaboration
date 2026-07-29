"""Transport boundary for cloud-to-edge raw-context requests."""

from __future__ import annotations

from typing import Protocol

import requests

from common.schemas import ContractError


class RawContextTransport(Protocol):
    def send(self, request: dict[str, object]) -> dict[str, object]:
        """Send one persisted context request and return the edge response."""


class HttpRawContextTransport:
    def __init__(self, edge_base_url: str, timeout_seconds: float = 3.0):
        self.url = (
            edge_base_url.rstrip("/") + "/edge/raw-context-requests"
        )
        self.timeout_seconds = timeout_seconds

    def send(self, request: dict[str, object]) -> dict[str, object]:
        response = requests.post(
            self.url,
            json=request,
            timeout=self.timeout_seconds,
        )
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            raise ContractError(
                "EDGE_REJECTED_CONTEXT_REQUEST",
                "edge rejected raw-context request",
            ) from error
        except ValueError as error:
            raise ContractError(
                "EDGE_REJECTED_CONTEXT_REQUEST",
                "edge response is not valid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise ContractError(
                "EDGE_REJECTED_CONTEXT_REQUEST",
                "edge raw-context response must be an object",
            )
        return payload
