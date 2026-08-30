"""Bearing input adapter backed by scenario-owned ingestion implementations."""

from __future__ import annotations

from dataclasses import replace
import itertools
from pathlib import Path
import re
from typing import Any, Protocol, cast

from sender.input_adapter import (
    InputWindow,
    PreparedScenarioInput,
)
from scenarios.bearing.ingestion.mat_reader import MatDataError, SignalWindow, load_mat_record
from scenarios.bearing.ingestion.packet import build_sensor_packet, serialize_packet
from scenarios.bearing.ingestion.source_mapping import PacketSourceMappingStore


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
        resolved_source = Path(source_path).resolve()
        if resolved_source.is_dir():
            return self._prepare_directory(
                resolved_source,
                unit_id=unit_id,
                duration_ms=duration_ms,
                count=count,
            )
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

    def _prepare_directory(
        self,
        source_dir: Path,
        *,
        unit_id: str,
        duration_ms: int,
        count: int,
    ) -> PreparedScenarioInput:
        source_files = sorted(
            (
                path.resolve()
                for path in source_dir.iterdir()
                if path.is_file() and path.suffix.casefold() == ".mat"
            ),
            key=_natural_path_key,
        )
        if len(source_files) != count:
            raise MatDataError(
                f"MAT directory must contain exactly {count} MAT files; "
                f"found {len(source_files)}: {source_dir}"
            )

        duration_seconds = duration_ms / 1000.0
        prepared_windows: list[SignalWindow] = []
        window_source_paths: dict[int, Path] = {}
        for sequence_number, mat_path in enumerate(source_files, start=1):
            record = load_mat_record(mat_path)
            windows = record.windows(duration_ms=duration_ms, count=1)
            try:
                window = next(windows)
            except StopIteration as exc:
                raise MatDataError(
                    f"MAT file produced no {duration_ms} ms window: {mat_path}"
                ) from exc
            _require_complete_window(window, mat_path, duration_ms)
            prepared_windows.append(
                replace(
                    window,
                    sequence_number=sequence_number,
                    start_seconds=(sequence_number - 1) * duration_seconds,
                    end_seconds=sequence_number * duration_seconds,
                    window_index=sequence_number - 1,
                )
            )
            window_source_paths[sequence_number] = mat_path

        return PreparedScenarioInput(
            source_path=source_dir,
            first_window=prepared_windows[0],
            windows=iter(prepared_windows),
            window_source_paths=window_source_paths,
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
        run_id: str | None = None,
    ) -> dict[str, Any]:
        packet_arguments: dict[str, Any] = {
            "device_id": device_id,
            "task_id": task_id,
            "bearing_id": unit_id,
            "sender_id": sender_id,
            "sequence_number": sequence_number,
            "data": window.data,
            "end_generate_timestamp_ns": end_generate_timestamp_ns,
        }
        if run_id is not None:
            packet_arguments["run_id"] = run_id
        return build_sensor_packet(**packet_arguments)

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


def _natural_path_key(path: Path) -> tuple[tuple[int, object], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", path.name)
        if part
    )


def _require_complete_window(
    window: SignalWindow,
    source_path: Path,
    duration_ms: int,
) -> None:
    for value in window.data.values():
        if not isinstance(value, dict) or "sample_rate_hz" not in value:
            continue
        expected_count = round(value["sample_rate_hz"] * duration_ms / 1000.0)
        samples = value.get("values")
        if (
            value.get("sample_count") != expected_count
            or not isinstance(samples, list)
            or len(samples) != expected_count
        ):
            raise MatDataError(
                f"MAT file does not contain a complete {duration_ms} ms window: "
                f"{source_path}"
            )
