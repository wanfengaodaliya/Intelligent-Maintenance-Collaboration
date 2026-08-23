"""Bearing MAT ingestion adapter around the existing sender implementation."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Protocol, cast

from sender.input_adapter import (
    InputWindow,
    PreparedScenarioInput,
)
from sender.packet import build_sensor_packet, serialize_packet
from sender.source_mapping import PacketSourceMappingStore
from scenarios.bearing.ingestion.mat_reader import load_mat_record


class BearingSourceMappingStore(Protocol):
    def save(
        self,
        *,
        packet_id: str,
        task_id: str,
        bearing_id: str,
        source_path: Path,
        start_index: int,
        end_index: int,
        window_index: int,
    ) -> None: ...


class BearingInputAdapter:
    def __init__(self, source_mapping_store: BearingSourceMappingStore):
        self._source_mapping_store = source_mapping_store

    def prepare(
        self,
        source_path: Path | str,
        *,
        unit_id: str,
        duration_ms: int,
        count: int,
    ) -> PreparedScenarioInput:
        record = load_mat_record(source_path)
        windows = record.windows(duration_ms=duration_ms, count=count)
        try:
            first_window = next(windows)
        except StopIteration as exc:
            raise RuntimeError(
                f"MAT record produced no data windows: {unit_id}"
            ) from exc
        return PreparedScenarioInput(
            source_path=record.source_path,
            first_window=first_window,
            windows=itertools.chain([first_window], windows),
        )

    def build_packet(
        self,
        *,
        device_id: str,
        task_id: str,
        unit_id: str,
        sender_id: str,
        sequence_number: int,
        window: InputWindow,
        end_generate_timestamp_ns: int,
    ) -> dict[str, Any]:
        return build_sensor_packet(
            device_id=device_id,
            task_id=task_id,
            bearing_id=unit_id,
            sender_id=sender_id,
            sequence_number=sequence_number,
            data=window.data,
            end_generate_timestamp_ns=end_generate_timestamp_ns,
        )

    def next_window(
        self,
        prepared_input: PreparedScenarioInput,
        *,
        unit_id: str,
        expected_sequence: int,
    ) -> InputWindow:
        try:
            window = next(prepared_input.windows)
        except StopIteration as exc:
            raise RuntimeError(
                f"MAT record ended before packet {expected_sequence}: {unit_id}"
            ) from exc
        if window.sequence_number != expected_sequence:
            raise RuntimeError(f"bearing window sequence mismatch: {unit_id}")
        return window

    def persist_source(
        self,
        *,
        packet: dict[str, Any],
        task_id: str,
        unit_id: str,
        source_path: Path,
        window: InputWindow,
    ) -> None:
        self._source_mapping_store.save(
            packet_id=packet["packet_id"],
            task_id=task_id,
            bearing_id=unit_id,
            source_path=source_path,
            start_index=window.start_index,
            end_index=window.end_index,
            window_index=window.window_index,
        )

    def serialize_packet(self, packet: dict[str, Any]) -> bytes:
        return serialize_packet(packet)


class BearingInputAdapterProvider:
    scenario_id = "bearing"

    def build_adapter(
        self,
        state_dir: Path,
        source_mapping_store: object | None = None,
    ) -> BearingInputAdapter:
        store = (
            cast(BearingSourceMappingStore, source_mapping_store)
            if source_mapping_store is not None
            else PacketSourceMappingStore(state_dir / "packet_source_mapping.db")
        )
        return BearingInputAdapter(store)
