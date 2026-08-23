from __future__ import annotations

import importlib
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
