"""Transport boundary for cloud-to-edge raw-context requests."""

from __future__ import annotations

from typing import Mapping, Protocol

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


class RoutedRawContextTransport:
    def __init__(
        self,
        default_edge_base_url: str,
        edge_base_urls: Mapping[str, str],
        timeout_seconds: float = 3.0,
    ):
        self.default_edge_base_url = default_edge_base_url.rstrip("/")
        self.edge_base_urls = {
            edge_node_id: base_url.rstrip("/")
            for edge_node_id, base_url in edge_base_urls.items()
        }
        self.timeout_seconds = timeout_seconds

    def send(self, request: dict[str, object]) -> dict[str, object]:
        edge_node_id = request.get("edge_node_id")
        if isinstance(edge_node_id, str) and self.edge_base_urls:
            try:
                base_url = self.edge_base_urls[edge_node_id]
            except KeyError as exc:
                raise ValueError(
                    f"unknown edge_node_id for raw-context routing: {edge_node_id}"
                ) from exc
        else:
            base_url = self.default_edge_base_url
        return HttpRawContextTransport(
            base_url,
            timeout_seconds=self.timeout_seconds,
        ).send(request)
