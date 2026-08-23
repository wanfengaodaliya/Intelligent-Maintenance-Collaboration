from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from scipy.io import savemat


INGESTION_MODULES = (
    (
        "mat_reader",
        (
            "MatDataError",
            "SignalSeries",
            "SignalWindow",
            "MatRecord",
            "SOURCE_TO_PACKET",
            "PACKET_UNITS",
            "TEMPERATURE_SOURCE",
            "load_mat_record",
        ),
    ),
    (
        "packet",
        (
            "PacketValidationError",
            "ARRAY_SIGNALS",
            "TEMPERATURE_SIGNAL",
            "TASK_ID_PATTERN",
            "SENDER_ID_PATTERN",
            "BEARING_ID_PATTERN",
            "build_sensor_packet",
            "serialize_packet",
        ),
    ),
    (
        "source_mapping",
        (
            "PADERBORN_FILENAME",
            "extract_paderborn_bearing_code",
            "PacketSourceMappingStore",
        ),
    ),
)


@pytest.mark.parametrize(("module_name", "public_names"), INGESTION_MODULES)
def test_legacy_ingestion_exports_are_scenario_objects(
    module_name: str,
    public_names: tuple[str, ...],
) -> None:
    scenario_module = importlib.import_module(
        f"scenarios.bearing.ingestion.{module_name}"
    )
    compatibility_module = importlib.import_module(
        "compatibility.bearing_v12.ingestion_exports"
    )
    legacy_module = importlib.import_module(f"sender.{module_name}")

    assert set(legacy_module.__all__) == set(public_names)
    assert set(public_names).issubset(compatibility_module.__all__)
    for public_name in public_names:
        scenario_value = getattr(scenario_module, public_name)
        assert getattr(compatibility_module, public_name) is scenario_value
        assert getattr(legacy_module, public_name) is scenario_value


def _write_fixed_paderborn_mat(path: Path) -> None:
    fast_times = np.arange(6400, dtype=float) / 64000
    slow_times = np.arange(400, dtype=float) / 4000
    temperature_times = np.array([0.0, 0.05, 0.1])
    x_axes = np.array(
        [
            {"Data": fast_times},
            {"Data": slow_times},
            {"Data": temperature_times},
        ],
        dtype=object,
    )
    signals = np.array(
        [
            {"Name": "vibration_1", "XIndex": 1, "Data": fast_times + 1},
            {"Name": "phase_current_1", "XIndex": 1, "Data": fast_times + 2},
            {"Name": "phase_current_2", "XIndex": 1, "Data": fast_times + 3},
            {"Name": "speed", "XIndex": 2, "Data": slow_times + 4},
            {"Name": "torque", "XIndex": 2, "Data": slow_times + 5},
            {"Name": "force", "XIndex": 2, "Data": slow_times + 6},
            {
                "Name": "temp_2_bearing_module",
                "XIndex": 3,
                "Data": np.array([20.0, 21.0, 22.0]),
            },
        ],
        dtype=object,
    )
    savemat(path, {"measurement": {"X": x_axes, "Y": signals}})


def test_mat_reader_preserves_fixed_record_and_window_outputs(tmp_path: Path) -> None:
    from sender.mat_reader import load_mat_record as legacy_load
    from scenarios.bearing.ingestion.mat_reader import load_mat_record

    mat_path = tmp_path / "N09_M07_F10_KA01_1.mat"
    _write_fixed_paderborn_mat(mat_path)

    record = load_mat_record(mat_path)
    legacy_record = legacy_load(mat_path)
    windows = list(record.windows(duration_ms=50, count=2))
    legacy_windows = list(legacy_record.windows(duration_ms=50, count=2))

    assert record.source_path == mat_path.resolve()
    assert set(record.series) == {
        "vibration",
        "phase_current_1_A",
        "phase_current_2_A",
        "shaft_speed_rpm",
        "load_torque_nm",
        "bearing_radial_load_n",
    }
    assert {name: series.sample_rate_hz for name, series in record.series.items()} == {
        "vibration": 64000,
        "phase_current_1_A": 64000,
        "phase_current_2_A": 64000,
        "shaft_speed_rpm": 4000,
        "load_torque_nm": 4000,
        "bearing_radial_load_n": 4000,
    }
    assert windows == legacy_windows
    assert [window.sequence_number for window in windows] == [1, 2]
    assert [(window.start_index, window.end_index) for window in windows] == [
        (0, 3200),
        (3200, 6400),
    ]
    assert [window.data["vibration"]["sample_count"] for window in windows] == [
        3200,
        3200,
    ]
    assert windows[0].data["vibration"]["unit"] == "mm/s"
    assert [window.data["bearing_module_temperature_c"] for window in windows] == [
        21.0,
        22.0,
    ]


def test_mat_reader_preserves_fixed_errors(tmp_path: Path) -> None:
    from sender.mat_reader import MatDataError as LegacyMatDataError
    from sender.mat_reader import load_mat_record as legacy_load
    from scenarios.bearing.ingestion.mat_reader import MatDataError, load_mat_record

    missing_path = tmp_path / "missing.mat"
    for loader in (load_mat_record, legacy_load):
        with pytest.raises(MatDataError) as missing_error:
            loader(missing_path)
        assert str(missing_error.value) == (
            f"MAT file does not exist: {missing_path.resolve()}"
        )

    invalid_path = tmp_path / "invalid.mat"
    savemat(invalid_path, {"unexpected": np.array([1.0])})
    for loader in (load_mat_record, legacy_load):
        with pytest.raises(LegacyMatDataError) as structure_error:
            loader(invalid_path)
        assert str(structure_error.value) == (
            "MAT file does not contain the expected Paderborn structure"
        )


def _fixed_packet_data() -> dict[str, object]:
    return {
        "vibration": {
            "sample_rate_hz": 64000,
            "sample_count": 1,
            "values": [1.0],
            "unit": "mm/s",
        },
        "phase_current_1_A": {
            "sample_rate_hz": 64000,
            "sample_count": 1,
            "values": [2.0],
            "unit": "A",
        },
        "phase_current_2_A": {
            "sample_rate_hz": 64000,
            "sample_count": 1,
            "values": [3.0],
            "unit": "A",
        },
        "shaft_speed_rpm": {
            "sample_rate_hz": 4000,
            "sample_count": 1,
            "values": [4.0],
        },
        "load_torque_nm": {
            "sample_rate_hz": 4000,
            "sample_count": 1,
            "values": [5.0],
        },
        "bearing_radial_load_n": {
            "sample_rate_hz": 4000,
            "sample_count": 1,
            "values": [6.0],
        },
        "bearing_module_temperature_c": 25.0,
    }


def _fixed_packet_arguments() -> dict[str, object]:
    return {
        "device_id": " machine_01 ",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "sender_id": "sender_01",
        "sequence_number": 7,
        "data": _fixed_packet_data(),
        "end_generate_timestamp_ns": 123,
    }


def test_packet_builder_preserves_dictionary_and_serialized_bytes() -> None:
    from sender.packet import build_sensor_packet as legacy_build
    from sender.packet import serialize_packet as legacy_serialize
    from scenarios.bearing.ingestion.packet import (
        build_sensor_packet,
        serialize_packet,
    )

    packet = build_sensor_packet(**_fixed_packet_arguments())
    legacy_packet = legacy_build(**_fixed_packet_arguments())

    assert packet == legacy_packet
    assert packet == {
        "device_id": "machine_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_id": "sd_01_tk_0001_bearing_01_pkt_007",
        "sender_id": "sender_01",
        "sequence_number": 7,
        "end_generate_timestamp_ns": 123,
        "data": _fixed_packet_data(),
    }
    expected_bytes = (
        b'{"device_id":"machine_01","task_id":"sd_01_tk_0001",'
        b'"bearing_id":"bearing_01","packet_id":'
        b'"sd_01_tk_0001_bearing_01_pkt_007","sender_id":"sender_01",'
        b'"sequence_number":7,"end_generate_timestamp_ns":123,"data":{'
        b'"vibration":{"sample_rate_hz":64000,"sample_count":1,'
        b'"values":[1.0],"unit":"mm/s"},"phase_current_1_A":{'
        b'"sample_rate_hz":64000,"sample_count":1,"values":[2.0],'
        b'"unit":"A"},"phase_current_2_A":{"sample_rate_hz":64000,'
        b'"sample_count":1,"values":[3.0],"unit":"A"},'
        b'"shaft_speed_rpm":{"sample_rate_hz":4000,"sample_count":1,'
        b'"values":[4.0]},"load_torque_nm":{"sample_rate_hz":4000,'
        b'"sample_count":1,"values":[5.0]},"bearing_radial_load_n":{'
        b'"sample_rate_hz":4000,"sample_count":1,"values":[6.0]},'
        b'"bearing_module_temperature_c":25.0}}'
    )
    assert serialize_packet(packet) == expected_bytes
    assert legacy_serialize(legacy_packet) == expected_bytes


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"task_id": "bad"}, "task_id must match sd_<sender>_tk_<number>"),
        ({"device_id": " "}, "device_id cannot be empty"),
        ({"bearing_id": "bad"}, "bearing_id must match bearing_<number>"),
        ({"sender_id": "bad"}, "sender_id must match sender_<number>"),
        ({"sender_id": "sender_02"}, "task_id does not belong to sender_id"),
        ({"sequence_number": 0}, "sequence_number must be between 1 and 999"),
        (
            {"end_generate_timestamp_ns": 0},
            "end_generate_timestamp_ns must be positive",
        ),
        ({"data": {}}, "missing signal: vibration"),
    ),
)
def test_packet_builder_preserves_validation_errors(
    overrides: dict[str, object],
    message: str,
) -> None:
    from sender.packet import PacketValidationError as LegacyPacketValidationError
    from sender.packet import build_sensor_packet as legacy_build
    from scenarios.bearing.ingestion.packet import (
        PacketValidationError,
        build_sensor_packet,
    )

    arguments = _fixed_packet_arguments()
    arguments.update(overrides)
    for builder in (build_sensor_packet, legacy_build):
        with pytest.raises(PacketValidationError) as error:
            builder(**arguments)
        assert isinstance(error.value, LegacyPacketValidationError)
        assert str(error.value) == message


def test_packet_serializer_preserves_validation_error() -> None:
    from sender.packet import PacketValidationError as LegacyPacketValidationError
    from sender.packet import serialize_packet as legacy_serialize
    from scenarios.bearing.ingestion.packet import (
        PacketValidationError,
        serialize_packet,
    )

    for serializer in (serialize_packet, legacy_serialize):
        with pytest.raises(PacketValidationError) as error:
            serializer({"invalid": float("nan")})
        assert isinstance(error.value, LegacyPacketValidationError)
        assert str(error.value) == (
            "packet cannot be serialized: "
            "Out of range float values are not JSON compliant: nan"
        )


def _create_historical_source_mapping_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE packet_source_mapping (
                   packet_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                   bearing_id TEXT NOT NULL,dataset_name TEXT NOT NULL,
                   dataset_version TEXT NOT NULL,source_file TEXT NOT NULL,
                   source_bearing_code TEXT NOT NULL,start_index INTEGER NOT NULL,
                   end_index INTEGER NOT NULL,window_index INTEGER NOT NULL,
                   created_at_ns INTEGER NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO packet_source_mapping VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "historical_packet",
                "sd_01_tk_0001",
                "bearing_01",
                "paderborn",
                "paderborn_v1",
                "N09_M07_F10_KA01_1.mat",
                "KA01",
                0,
                3200,
                0,
                100,
            ),
        )


def _source_mapping_created_at(database_path: Path, packet_id: str) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT created_at_ns FROM packet_source_mapping WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_source_mapping_preserves_historical_database_and_upsert_shape(
    tmp_path: Path,
) -> None:
    from sender.source_mapping import PacketSourceMappingStore as LegacyStore
    from scenarios.bearing.ingestion.source_mapping import PacketSourceMappingStore

    database_path = tmp_path / "historical.db"
    _create_historical_source_mapping_database(database_path)

    legacy_store = LegacyStore(database_path)
    scenario_store = PacketSourceMappingStore(database_path)
    historical_mapping = {
        "packet_id": "historical_packet",
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

    assert legacy_store.get("historical_packet") == historical_mapping
    assert scenario_store.get("historical_packet") == historical_mapping

    legacy_store.save(
        packet_id="new_packet",
        task_id="sd_01_tk_0002",
        bearing_id="bearing_01",
        source_path=tmp_path / "N09_M07_F10_KA01_1.mat",
        start_index=3200,
        end_index=6400,
        window_index=1,
    )
    created_at_ns = _source_mapping_created_at(database_path, "new_packet")
    scenario_store.save(
        packet_id="new_packet",
        task_id="sd_01_tk_0003",
        bearing_id="bearing_02",
        source_path=tmp_path / "N09_M07_F10_ki04_1.mat",
        start_index=6400,
        end_index=9600,
        window_index=2,
    )

    assert legacy_store.get("new_packet") == {
        "packet_id": "new_packet",
        "task_id": "sd_01_tk_0003",
        "bearing_id": "bearing_02",
        "dataset_name": "paderborn",
        "dataset_version": "paderborn_v1",
        "source_file": "N09_M07_F10_ki04_1.mat",
        "source_bearing_code": "KI04",
        "start_index": 6400,
        "end_index": 9600,
        "window_index": 2,
    }
    assert _source_mapping_created_at(database_path, "new_packet") == created_at_ns


def test_source_mapping_preserves_filename_error() -> None:
    from sender.source_mapping import extract_paderborn_bearing_code as legacy_extract
    from scenarios.bearing.ingestion.source_mapping import (
        extract_paderborn_bearing_code,
    )

    for extractor in (extract_paderborn_bearing_code, legacy_extract):
        with pytest.raises(ValueError) as error:
            extractor("unsupported.mat")
        assert str(error.value) == (
            "source file is not a supported Paderborn MAT filename"
        )
