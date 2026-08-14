from __future__ import annotations

import json
from pathlib import Path

from core.diagnosis_identity import (
    build_decision_round_id,
    build_diagnosis_window_id,
    canonical_json,
)


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "diagnosis_identity.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_golden_identity_fixture_is_stable() -> None:
    fixture = _fixture()
    identity = fixture["identity"]
    assert isinstance(identity, dict)

    assert build_diagnosis_window_id(**identity) == fixture["diagnosis_window_id"]
    assert build_decision_round_id(
        device_id=identity["device_id"],
        task_id=identity["task_id"],
        window_start_sequence=identity["window_start_sequence"],
        window_end_sequence=identity["window_end_sequence"],
    ) == fixture["decision_round_id"]


def test_canonical_json_ignores_mapping_insertion_order() -> None:
    first = {"bearing_id": "bearing_02", "device_id": "machine_01"}
    second = {"device_id": "machine_01", "bearing_id": "bearing_02"}
    assert canonical_json(first) == canonical_json(second)


def test_round_identity_excludes_bearing_and_sender() -> None:
    original = _fixture()["identity"]
    assert isinstance(original, dict)
    first = build_decision_round_id(
        device_id=original["device_id"],
        task_id=original["task_id"],
        window_start_sequence=original["window_start_sequence"],
        window_end_sequence=original["window_end_sequence"],
    )
    second = build_decision_round_id(
        device_id=original["device_id"],
        task_id=original["task_id"],
        window_start_sequence=original["window_start_sequence"],
        window_end_sequence=original["window_end_sequence"],
    )
    assert first == second
