from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from sender import __main__ as cli


@dataclass(frozen=True)
class ConfigStub:
    device_id: str


def test_device_id_increments_and_preserves_suffix_width() -> None:
    assert cli.device_id_for_round("machine_01", 1, 2) == "machine_01"
    assert cli.device_id_for_round("machine_01", 2, 2) == "machine_02"
    assert cli.device_id_for_round("machine_099", 2, 2) == "machine_100"


def test_multiple_rounds_require_a_numeric_suffix() -> None:
    with pytest.raises(ValueError, match="numeric suffix"):
        cli.device_id_for_round("machine", 1, 2)


def test_formal_round_count_runs_45_sender_tasks(monkeypatch, capsys) -> None:
    calls: list[tuple[object, object, bool]] = []

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: ConfigStub(device_id="machine_01"),
    )
    monkeypatch.setattr(
        cli,
        "parse_source_files",
        lambda _entries: {
            "sender_01": "a.mat",
            "sender_02": "b.mat",
            "sender_03": "c.mat",
        },
    )

    def run_round(config, source_files, *, realtime):
        calls.append((config, source_files, realtime))
        return [{"task_status": "completed"}] * 3

    monkeypatch.setattr(cli, "run_all_senders", run_round)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sender",
            "--rounds",
            "15",
            "--source",
            "sender_01=a.mat",
            "--source",
            "sender_02=b.mat",
            "--source",
            "sender_03=c.mat",
        ],
    )

    assert cli.main() == 0
    assert len(calls) == 15
    assert [config.device_id for config, _sources, _realtime in calls] == [
        f"machine_{number:02d}" for number in range(1, 16)
    ]
    assert all(realtime is True for _config, _sources, realtime in calls)
    assert capsys.readouterr().err == ""
