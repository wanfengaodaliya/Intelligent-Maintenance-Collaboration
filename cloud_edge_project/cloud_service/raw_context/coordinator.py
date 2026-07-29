"""Create, persist, and dispatch raw-context requests."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.schemas import ContractError

from cloud_service.storage.raw_context_repository import (
    RawContextRequestRepository,
)

from .contracts import validate_edge_context_response
from .transport import RawContextTransport


class RawContextCoordinator:
    def __init__(
        self,
        database_path: Path,
        *,
        transport: RawContextTransport,
        clock_ns: Callable[[], int] = time.time_ns,
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        self.repository = RawContextRequestRepository(database_path)
        self.transport = transport
        self.clock_ns = clock_ns
        self.request_id_factory = request_id_factory

    def create_and_dispatch(
        self,
        *,
        review_id: str,
        task_id: str,
        sender_id: str,
        anchor_packet_id: str,
        anchor_sequence_number: int,
    ) -> dict[str, Any]:
        now = self.clock_ns()
        stored = self.repository.create_or_get(
            request_id=self.request_id_factory(),
            review_id=review_id,
            task_id=task_id,
            sender_id=sender_id,
            anchor_packet_id=anchor_packet_id,
            anchor_sequence_number=anchor_sequence_number,
            before_packet_count=10,
            after_packet_count=10,
            requested_at_ns=now,
            deadline_at_ns=now + 3_000_000_000,
        )
        if stored["request_status"] not in {"created", "dispatch_failed"}:
            return stored
        request = _request_payload(stored)
        try:
            response = self.transport.send(request)
            validated = validate_edge_context_response(
                response,
                request_id=stored["request_id"],
                anchor_packet_id=stored["anchor_packet_id"],
            )
        except ContractError as error:
            return self.repository.update_dispatch(
                stored["request_id"],
                request_status="dispatch_failed",
                last_error_code=error.code,
                updated_at_ns=self.clock_ns(),
            )
        except Exception:
            return self.repository.update_dispatch(
                stored["request_id"],
                request_status="dispatch_failed",
                last_error_code="EDGE_UNAVAILABLE",
                updated_at_ns=self.clock_ns(),
            )
        if validated["status"] == "insufficient_context":
            return self.repository.mark_insufficient(
                stored["request_id"],
                error_code="EDGE_INSUFFICIENT_CONTEXT",
                edge_response=validated,
                updated_at_ns=self.clock_ns(),
            )
        return self.repository.update_dispatch(
            stored["request_id"],
            request_status="pending_context",
            edge_response=validated,
            updated_at_ns=self.clock_ns(),
        )


def _request_payload(stored: dict[str, Any]) -> dict[str, object]:
    return {
        "request_id": stored["request_id"],
        "task_id": stored["task_id"],
        "sender_id": stored["sender_id"],
        "anchor_packet_id": stored["anchor_packet_id"],
        "anchor_sequence_number": stored["anchor_sequence_number"],
        "before_packet_count": stored["before_packet_count"],
        "after_packet_count": stored["after_packet_count"],
        "requested_at_ns": stored["requested_at_ns"],
        "deadline_at_ns": stored["deadline_at_ns"],
    }
