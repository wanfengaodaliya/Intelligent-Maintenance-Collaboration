from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap.scenarios import build_sender_scenario_registry
from core.scenario_plugin import INPUT_ADAPTER
from sender.mat_reader import MatDataError, SignalWindow
from sender.packet import build_sensor_packet, serialize_packet
from sender.source_mapping import PacketSourceMappingStore
from scenarios.bearing.ingestion import (
    BearingInputAdapter,
    BearingInputAdapterProvider,
)
from scenarios.bearing.ingestion import provider as ingestion_module


def _window() -> SignalWindow:
    signals = {
        name: {"sample_rate_hz": rate, "sample_count": 1, "values": [1.0]}
        for name, rate in {
            "vibration": 64000,
            "phase_current_1_A": 64000,
            "phase_current_2_A": 64000,
            "shaft_speed_rpm": 4000,
            "load_torque_nm": 4000,
            "bearing_radial_load_n": 4000,
        }.items()
    }
    signals["vibration"]["unit"] = "mm/s"
    signals["phase_current_1_A"]["unit"] = "A"
    signals["phase_current_2_A"]["unit"] = "A"
    signals["bearing_module_temperature_c"] = 25.0
    return SignalWindow(1, 0.0, 0.05, 0, 3200, 0, signals)


def _complete_window() -> SignalWindow:
    window = _window()
    data = dict(window.data)
    for name, value in tuple(data.items()):
        if not isinstance(value, dict) or "sample_rate_hz" not in value:
            continue
        channel = dict(value)
        count = round(channel["sample_rate_hz"] * 0.05)
        channel["sample_count"] = count
        channel["values"] = [1.0] * count
        data[name] = channel
    return SignalWindow(1, 0.0, 0.05, 0, 3200, 0, data)


def test_bearing_input_adapter_replays_directory_in_natural_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "K004"
    source_dir.mkdir()
    for name in (
        "N09_M07_F10_K004_10.mat",
        "N09_M07_F10_K004_2.mat",
        "N09_M07_F10_K004_1.mat",
    ):
        (source_dir / name).touch()

    loaded_paths: list[Path] = []

    def load_record(path: Path | str) -> SimpleNamespace:
        resolved = Path(path).resolve()
        loaded_paths.append(resolved)
        return SimpleNamespace(
            source_path=resolved,
            windows=lambda **kwargs: iter([_complete_window()]),
        )

    monkeypatch.setattr(ingestion_module, "load_mat_record", load_record)
    adapter = BearingInputAdapter(
        PacketSourceMappingStore(tmp_path / "mapping.db")
    )

    prepared = adapter.prepare(
        source_dir,
        unit_id="bearing_01",
        duration_ms=50,
        count=3,
    )
    windows = list(prepared.windows)

    assert [path.name for path in loaded_paths] == [
        "N09_M07_F10_K004_1.mat",
        "N09_M07_F10_K004_2.mat",
        "N09_M07_F10_K004_10.mat",
    ]
    assert [window.sequence_number for window in windows] == [1, 2, 3]
    assert [window.window_index for window in windows] == [0, 1, 2]
    assert [prepared.window_source_paths[index].name for index in (1, 2, 3)] == [
        "N09_M07_F10_K004_1.mat",
        "N09_M07_F10_K004_2.mat",
        "N09_M07_F10_K004_10.mat",
    ]


def test_bearing_input_adapter_keeps_condition_groups_separate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "KA09"
    source_dir.mkdir()
    names = (
        "N15_M07_F10_KA09_1.mat",
        "N09_M07_F10_KA09_20.mat",
        "N15_M01_F10_KA09_1.mat",
        "N15_M07_F04_KA09_20.mat",
        "N15_M07_F04_KA09_1.mat",
    )
    for name in names:
        (source_dir / name).touch()

    loaded_paths: list[Path] = []

    def load_record(path: Path | str) -> SimpleNamespace:
        resolved = Path(path).resolve()
        loaded_paths.append(resolved)
        return SimpleNamespace(
            source_path=resolved,
            windows=lambda **kwargs: iter([_complete_window()]),
        )

    monkeypatch.setattr(ingestion_module, "load_mat_record", load_record)
    adapter = BearingInputAdapter(
        PacketSourceMappingStore(tmp_path / "mapping.db")
    )

    adapter.prepare(
        source_dir,
        unit_id="bearing_01",
        duration_ms=50,
        count=len(names),
    )

    assert [path.name for path in loaded_paths] == [
        "N09_M07_F10_KA09_20.mat",
        "N15_M01_F10_KA09_1.mat",
        "N15_M07_F04_KA09_1.mat",
        "N15_M07_F04_KA09_20.mat",
        "N15_M07_F10_KA09_1.mat",
    ]


def test_bearing_input_adapter_requires_exact_directory_mat_count(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "K004"
    source_dir.mkdir()
    (source_dir / "N09_M07_F10_K004_1.mat").touch()
    (source_dir / "N09_M07_F10_K004_2.mat").touch()
    adapter = BearingInputAdapter(
        PacketSourceMappingStore(tmp_path / "mapping.db")
    )

    with pytest.raises(MatDataError, match="exactly 3 MAT files"):
        adapter.prepare(
            source_dir,
            unit_id="bearing_01",
            duration_ms=50,
            count=3,
        )


def test_bearing_input_adapter_rejects_incomplete_directory_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "K004"
    source_dir.mkdir()
    source_file = source_dir / "N09_M07_F10_K004_1.mat"
    source_file.touch()
    monkeypatch.setattr(
        ingestion_module,
        "load_mat_record",
        lambda path: SimpleNamespace(
            source_path=Path(path).resolve(),
            windows=lambda **kwargs: iter([_window()]),
        ),
    )
    adapter = BearingInputAdapter(
        PacketSourceMappingStore(tmp_path / "mapping.db")
    )

    with pytest.raises(MatDataError, match="complete 50 ms window"):
        adapter.prepare(
            source_dir,
            unit_id="bearing_01",
            duration_ms=50,
            count=1,
        )


def test_bearing_input_adapter_preserves_packet_bytes_and_source_mapping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    window = _window()
    monkeypatch.setattr(
        ingestion_module,
        "load_mat_record",
        lambda path: SimpleNamespace(
            source_path=source_path,
            windows=lambda **kwargs: iter([window]),
        ),
    )
    store = PacketSourceMappingStore(tmp_path / "mapping.db")
    adapter = BearingInputAdapter(store)
    prepared = adapter.prepare(
        source_path,
        unit_id="bearing_01",
        duration_ms=50,
        count=1,
    )

    packet = adapter.build_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        unit_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        window=prepared.first_window,
        end_generate_timestamp_ns=1,
    )
    legacy_packet = build_sensor_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        bearing_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        data=window.data,
        end_generate_timestamp_ns=1,
    )
    adapter.persist_source(
        packet=packet,
        task_id="sd_01_tk_0001",
        unit_id="bearing_01",
        source_path=prepared.source_path,
        window=prepared.first_window,
    )

    assert packet == legacy_packet
    assert adapter.serialize_packet(packet) == serialize_packet(legacy_packet)
    assert [item.sequence_number for item in prepared.windows] == [1]
    assert store.get(packet["packet_id"])["source_bearing_code"] == "KA01"


def test_bearing_input_adapter_preserves_window_validation_errors(
    tmp_path: Path,
) -> None:
    adapter = BearingInputAdapter(PacketSourceMappingStore(tmp_path / "mapping.db"))
    prepared = SimpleNamespace(windows=iter([]))

    with pytest.raises(
        RuntimeError,
        match="MAT record ended before packet 2: bearing_01",
    ):
        adapter.next_window(
            prepared,
            unit_id="bearing_01",
            expected_sequence=2,
        )

    mismatched = SimpleNamespace(windows=iter([_window()]))
    with pytest.raises(
        RuntimeError,
        match="bearing window sequence mismatch: bearing_01",
    ):
        adapter.next_window(
            mismatched,
            unit_id="bearing_01",
            expected_sequence=2,
        )


def test_sender_registry_runs_complete_bearing_ingestion_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    window = _window()
    monkeypatch.setattr(
        ingestion_module,
        "load_mat_record",
        lambda path: SimpleNamespace(
            source_path=source_path,
            windows=lambda **kwargs: iter([window]),
        ),
    )
    store = PacketSourceMappingStore(tmp_path / "mapping.db")
    registry = build_sender_scenario_registry()
    provider = registry.require_provider("bearing", INPUT_ADAPTER)

    assert isinstance(provider, BearingInputAdapterProvider)
    adapter = provider.build_adapter(tmp_path, store)
    prepared = adapter.prepare(
        source_path,
        unit_id="bearing_01",
        duration_ms=50,
        count=1,
    )
    first_window = adapter.next_window(
        prepared,
        unit_id="bearing_01",
        expected_sequence=1,
    )
    packet = adapter.build_packet(
        device_id="machine_01",
        task_id="sd_01_tk_0001",
        unit_id="bearing_01",
        sender_id="sender_01",
        sequence_number=1,
        window=first_window,
        end_generate_timestamp_ns=1,
    )
    adapter.persist_source(
        packet=packet,
        task_id="sd_01_tk_0001",
        unit_id="bearing_01",
        source_path=prepared.source_path,
        window=first_window,
    )

    assert adapter.serialize_packet(packet) == serialize_packet(packet)
    assert store.get(packet["packet_id"]) == {
        "packet_id": "sd_01_tk_0001_bearing_01_pkt_001",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "dataset_name": "paderborn",
        "dataset_version": "paderborn_v1",
        "source_file": "N09_M07_F10_KA01_1.mat",
        "source_bearing_code": "KA01",
        "start_index": 0,
        "end_index": 3200,
        "window_index": 0,
    }
