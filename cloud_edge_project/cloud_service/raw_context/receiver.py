"""Receive, validate, and persist edge raw-context batches."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.schemas import ContractError

from cloud_service.storage.cloud_review_repository import CloudReviewRepository
from cloud_service.storage.database import initialize_database
from cloud_service.storage.raw_context_repository import (
    RawContextRequestRepository,
    TERMINAL_REQUEST_STATUSES,
)
from cloud_service.storage.raw_packet_repository import RawPacketRepository

from .contracts import (
    validate_raw_context_batch_envelope,
    validate_raw_context_packet,
)

PROCESSING_LEASE_DURATION_NS = 30_000_000_000
PROCESSING_LEASE_WAIT_SECONDS = 5.0


class RawContextReceiver:
    def __init__(
        self,
        database_path: Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
    ):
        self.database_path = Path(database_path)
        self.clock_ns = clock_ns
        initialize_database(self.database_path)
        self.requests = RawContextRequestRepository(self.database_path)
        self.raw_packets = RawPacketRepository(self.database_path)
        self.reviews = CloudReviewRepository(self.database_path)

    def receive_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch = validate_raw_context_batch_envelope(payload)
        request = self.requests.get(batch["request_id"])
        if request is None:
            raise ContractError(
                "UNKNOWN_CONTEXT_REQUEST",
                "raw-context request does not exist",
            )
        received_at_ns = self.clock_ns()
        if received_at_ns > request["deadline_at_ns"]:
            self.requests.expire_due(now_ns=received_at_ns)
            raise ContractError(
                "CONTEXT_REQUEST_EXPIRED",
                "raw-context request deadline has passed",
            )
        self._validate_request_match(batch, request)
        anchor_end_timestamp_ns = self.raw_packets.end_timestamp(
            sender_id=request["sender_id"],
            packet_id=request["anchor_packet_id"],
        )
        if anchor_end_timestamp_ns is None:
            raise ContractError(
                "CONTEXT_REQUEST_MISMATCH",
                "anchor raw packet does not exist",
            )
        self._acquire_processing_lease(
            request,
            received_at_ns=received_at_ns,
        )
        results: list[dict[str, Any]] = []
        seen_packet_ids: set[str] = set()
        seen_sequences: set[int] = set()
        for candidate in batch["packets"]:
            packet_id = _packet_id(candidate)
            sequence_number = _sequence_number(candidate)
            try:
                packet = validate_raw_context_packet(
                    candidate,
                    batch=batch,
                    anchor_end_timestamp_ns=anchor_end_timestamp_ns,
                    before_packet_count=request["before_packet_count"],
                    after_packet_count=request["after_packet_count"],
                )
                if (
                    packet["packet_id"] in seen_packet_ids
                    or packet["sequence_number"] in seen_sequences
                ):
                    raise ContractError(
                        "INVALID_CONTEXT_PACKET",
                        "packet identifiers must be unique within a batch",
                        packet["packet_id"],
                    )
                seen_packet_ids.add(packet["packet_id"])
                seen_sequences.add(packet["sequence_number"])
                relative_position = (
                    packet["sequence_number"]
                    - request["anchor_sequence_number"]
                )
                ingest_status, ingest_error = (
                    self.raw_packets.ingest_context(
                        packet,
                        review_id=request["review_id"],
                        relative_position=relative_position,
                        role=batch["context_position"],
                    )
                )
                if ingest_status == "conflict":
                    results.append(
                        _item_result(
                            packet["packet_id"],
                            packet["sequence_number"],
                            "conflict",
                            ingest_error,
                        )
                    )
                    continue
                results.append(
                    _item_result(
                        packet["packet_id"],
                        packet["sequence_number"],
                        ingest_status,
                    )
                )
            except ContractError as error:
                results.append(
                    _item_result(
                        packet_id,
                        sequence_number,
                        "rejected",
                        error.code,
                    )
                )

        if self._is_complete(request):
            current = self.requests.mark_complete(
                request["request_id"],
                updated_at_ns=received_at_ns,
            )
        else:
            warning = (
                "EDGE_INSUFFICIENT_CONTEXT"
                if (
                    batch["context_status"] == "insufficient_context"
                    or batch["missing_sequence_numbers"]
                )
                else None
            )
            current = self.requests.update_dispatch(
                request["request_id"],
                request_status="pending_context",
                last_error_code=warning,
                updated_at_ns=received_at_ns,
            )
        context_status = _context_status(current["request_status"])
        accepted = any(
            result["status"] in {"accepted", "duplicate"}
            for result in results
        )
        return {
            "request_id": batch["request_id"],
            "batch_id": batch["batch_id"],
            "status": "accepted" if accepted else "rejected",
            "context_status": context_status,
            "results": results,
        }

    def _acquire_processing_lease(
        self,
        request: dict[str, Any],
        *,
        received_at_ns: int,
    ) -> None:
        if request["request_status"] in TERMINAL_REQUEST_STATUSES:
            return
        wait_until = time.monotonic() + PROCESSING_LEASE_WAIT_SECONDS
        while True:
            if self.requests.acquire_processing_lease(
                request["request_id"],
                lease_until_ns=(
                    received_at_ns + PROCESSING_LEASE_DURATION_NS
                ),
                updated_at_ns=received_at_ns,
            ):
                return
            current = self.requests.get(request["request_id"])
            if (
                current is not None
                and current["request_status"] in TERMINAL_REQUEST_STATUSES
            ):
                raise ContractError(
                    "CONTEXT_REQUEST_BUSY",
                    "raw-context request finalized before lease acquisition",
                )
            if time.monotonic() >= wait_until:
                raise ContractError(
                    "CONTEXT_REQUEST_BUSY",
                    "raw-context request is already processing a batch",
                )
            time.sleep(0.001)

    @staticmethod
    def _validate_request_match(
        batch: dict[str, Any], request: dict[str, Any]
    ) -> None:
        for field in (
            "device_id",
            "bearing_id",
            "sender_id",
            "anchor_packet_id",
            "anchor_sequence_number",
        ):
            if batch[field] != request[field]:
                raise ContractError(
                    "CONTEXT_REQUEST_MISMATCH",
                    f"{field} does not match raw-context request",
                )
        if (
            batch["context_position"] == "after"
            and request["after_packet_count"] == 0
        ):
            raise ContractError(
                "INVALID_CONTEXT_BATCH",
                "after context was not requested",
            )
        anchor = request["anchor_sequence_number"]
        if batch["context_position"] == "before":
            low = anchor - request["before_packet_count"]
            high = anchor - 1
        else:
            low = anchor + 1
            high = anchor + request["after_packet_count"]
        if (
            batch["first_sequence_number"] < low
            or batch["last_sequence_number"] > high
        ):
            raise ContractError(
                "CONTEXT_REQUEST_MISMATCH",
                "batch sequence range is outside the request",
            )

    def _is_complete(self, request: dict[str, Any]) -> bool:
        before = set(
            range(
                -request["before_packet_count"],
                0,
            )
        )
        after = set(
            range(
                1,
                request["after_packet_count"] + 1,
            )
        )
        return self.reviews.context_positions(
            request["review_id"]
        ) == before | after


def _item_result(
    packet_id: str | None,
    sequence_number: int | None,
    status: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "packet_id": packet_id,
        "sequence_number": sequence_number,
        "status": status,
    }
    if error_code:
        result["error_code"] = error_code
    return result


def _packet_id(candidate: Any) -> str | None:
    if isinstance(candidate, dict) and isinstance(
        candidate.get("packet_id"), str
    ):
        return candidate["packet_id"]
    return None


def _sequence_number(candidate: Any) -> int | None:
    if (
        isinstance(candidate, dict)
        and isinstance(candidate.get("sequence_number"), int)
        and not isinstance(candidate.get("sequence_number"), bool)
    ):
        return candidate["sequence_number"]
    return None


def _context_status(request_status: str) -> str:
    if request_status == "complete":
        return "complete"
    if request_status == "partial_context":
        return "partial_context"
    if request_status == "insufficient_context":
        return "insufficient_context"
    return "pending_context"
