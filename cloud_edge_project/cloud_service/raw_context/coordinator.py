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
from cloud_service.storage.raw_packet_repository import RawPacketRepository

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
        self.raw_packets = RawPacketRepository(database_path)
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
            before_packet_count=20,
            after_packet_count=0,
            minimum_context_packet_count=16,
            requested_at_ns=now,
            deadline_at_ns=now + 3_000_000_000,
        )
        if stored["request_status"] not in {"created", "dispatch_failed"}:
            return stored
        anchor_end_generate_timestamp_ns = self.raw_packets.end_timestamp(
            sender_id=stored["sender_id"],
            packet_id=stored["anchor_packet_id"],
        )
        if anchor_end_generate_timestamp_ns is None:
            return self.repository.update_dispatch(
                stored["request_id"],
                request_status="dispatch_failed",
                last_error_code="ANCHOR_RAW_PACKET_NOT_FOUND",
                updated_at_ns=self.clock_ns(),
            )
        request = _request_payload(
            stored,
            anchor_end_generate_timestamp_ns,
        )
        try:
            response = self.transport.send(request)
            validated = validate_edge_context_response(
                response,
                request_id=stored["request_id"],
                anchor_packet_id=stored["anchor_packet_id"],
                before_packet_count=stored["before_packet_count"],
                after_packet_count=stored["after_packet_count"],
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
            return self.repository.update_dispatch(
                stored["request_id"],
                request_status="pending_context",
                edge_response=validated,
                last_error_code="EDGE_INSUFFICIENT_CONTEXT",
                updated_at_ns=self.clock_ns(),
            )
        return self.repository.update_dispatch(
            stored["request_id"],
            request_status="pending_context",
            edge_response=validated,
            updated_at_ns=self.clock_ns(),
        )


def _request_payload(
    stored: dict[str, Any],
    anchor_end_generate_timestamp_ns: int,
) -> dict[str, object]:
    return {
        "request_id": stored["request_id"],
        "sender_id": stored["sender_id"],
        "anchor_packet_id": stored["anchor_packet_id"],
        "anchor_end_generate_timestamp_ns": (
            anchor_end_generate_timestamp_ns
        ),
        "before_packet_count": stored["before_packet_count"],
        "requested_at_ns": stored["requested_at_ns"],
    }
