from __future__ import annotations

import importlib

import pytest


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
