"""Generic sender-side contract for scenario input preparation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol


class InputWindow(Protocol):
    sequence_number: int
    start_index: int
    end_index: int
    window_index: int
    data: dict[str, object]


@dataclass(frozen=True)
class PreparedScenarioInput:
    source_path: Path
    first_window: InputWindow
    windows: Iterator[InputWindow]


class SenderInputAdapter(Protocol):
    def prepare(
        self,
        source_path: Path | str,
        *,
        unit_id: str,
        duration_ms: int,
        count: int,
    ) -> PreparedScenarioInput: ...

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
    ) -> dict[str, Any]: ...

    def next_window(
        self,
        prepared_input: PreparedScenarioInput,
        *,
        unit_id: str,
        expected_sequence: int,
    ) -> InputWindow: ...

    def persist_source(
        self,
        *,
        packet: dict[str, Any],
        task_id: str,
        unit_id: str,
        source_path: Path,
        window: InputWindow,
    ) -> None: ...

    def serialize_packet(self, packet: dict[str, Any]) -> bytes: ...


class SenderInputAdapterProvider(Protocol):
    scenario_id: str

    def build_adapter(
        self,
        state_dir: Path,
        source_mapping_store: object | None = None,
    ) -> SenderInputAdapter: ...
