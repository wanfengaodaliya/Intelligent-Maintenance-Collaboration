from __future__ import annotations

import sys

from sender import __main__ as cli


def test_formal_round_count_runs_45_sender_tasks(monkeypatch, capsys) -> None:
    calls: list[tuple[object, object, bool]] = []

    monkeypatch.setattr(cli, "load_config", lambda _path: "config")
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
    assert all(realtime is True for _config, _sources, realtime in calls)
    assert capsys.readouterr().err == ""
