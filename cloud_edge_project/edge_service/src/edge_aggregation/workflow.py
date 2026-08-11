"""Synchronous business workflow over asynchronous-capable cloud review gateways."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from core.bearing_workflow_contracts import (
    FINAL_CLOUD,
    REVIEW_SUCCEEDED,
    BearingTaskResult,
    BearingWindowResult,
    DeviceTaskResult,
    FinalPacketResult,
)
from edge_validation_cache import EdgeValidationCache

from .device import DeviceTaskAggregator, build_bearing_task_result
from .window import BearingWindowAggregator, WindowConflictPolicy


class CloudReviewGateway(Protocol):
    def review_packet(
        self, packet: FinalPacketResult, raw_packet: dict[str, Any]
    ) -> FinalPacketResult: ...

    def review_bearing_window(
        self, window: BearingWindowResult, raw_packets: list[dict[str, Any]]
    ) -> BearingWindowResult: ...

    def review_device(self, result: DeviceTaskResult) -> DeviceTaskResult: ...


class BearingAggregationWorkflow:
    """Advance only final results; cloud waits do not leak provisional data downstream."""

    def __init__(
        self,
        *,
        cache: EdgeValidationCache,
        cloud: CloudReviewGateway,
        packet_cloud_confidence_threshold: float = 0.80,
        window_policy: WindowConflictPolicy | None = None,
    ):
        self.cache = cache
        self.cloud = cloud
        self.packet_cloud_confidence_threshold = packet_cloud_confidence_threshold
        self.windows = BearingWindowAggregator(window_policy)
        self.devices = DeviceTaskAggregator()
        self._bearing_results: dict[tuple[str, str, str], BearingTaskResult] = {}

    def register_task(
        self, device_id: str, task_id: str, expected_bearing_ids: tuple[str, ...]
    ) -> None:
        self.devices.register_task(device_id, task_id, expected_bearing_ids)

    def accept_packet(self, packet: FinalPacketResult) -> DeviceTaskResult | None:
        final_packet = self._finalize_packet(packet)
        window = self.windows.add_packet(final_packet)
        if window is None:
            return None
        if window.review_required:
            window = self._review_window(window)
            self.windows.finalize_window(window)
        final_windows = self.windows.final_windows(
            final_packet.device_id, final_packet.task_id, final_packet.bearing_id
        )
        if len(final_windows) != 4:
            return None
        bearing = build_bearing_task_result(final_windows)
        bearing_key = (bearing.device_id, bearing.task_id, bearing.bearing_id)
        previous = self._bearing_results.get(bearing_key)
        if previous is not None and previous != bearing:
            raise ValueError("BEARING_RESULT_CONFLICT")
        self._bearing_results[bearing_key] = bearing
        device = self.devices.add_bearing_result(bearing)
        if device.status == "REVIEW_REQUIRED":
            return self.cloud.review_device(device)
        return device

    def _finalize_packet(self, packet: FinalPacketResult) -> FinalPacketResult:
        if packet.confidence >= self.packet_cloud_confidence_threshold:
            return packet
        raw_ref = self.cache.raw_ref_from_uri(packet.raw_data_ref)
        if not self.cache.pin(raw_ref):
            raise ValueError("DATA_REFERENCE_NOT_FOUND")
        try:
            raw_packet = self.cache.read(raw_ref)
            if raw_packet is None:
                raise ValueError("DATA_REFERENCE_NOT_FOUND")
            reviewed = self.cloud.review_packet(packet, raw_packet)
        finally:
            self.cache.unpin(raw_ref)
        if reviewed.decision_source != FINAL_CLOUD:
            raise ValueError("cloud packet review did not return FINAL_CLOUD")
        return reviewed

    def _review_window(self, window: BearingWindowResult) -> BearingWindowResult:
        refs = tuple(self.cache.raw_ref_from_uri(uri) for uri in window.raw_data_refs)
        if len(refs) != 20 or not self.cache.pin_many(refs):
            raise ValueError("WINDOW_DATA_REFERENCE_NOT_FOUND")
        try:
            raw_packets = []
            for ref in refs:
                packet = self.cache.read(ref)
                if packet is None:
                    raise ValueError("WINDOW_DATA_REFERENCE_NOT_FOUND")
                raw_packets.append(packet)
            reviewed = self.cloud.review_bearing_window(window, raw_packets)
        finally:
            self.cache.unpin_many(refs)
        if reviewed.result_source != FINAL_CLOUD:
            reviewed = replace(reviewed, result_source=FINAL_CLOUD)
        if reviewed.review_required or reviewed.review_status != REVIEW_SUCCEEDED:
            raise ValueError("cloud window review did not return a final result")
        return reviewed
