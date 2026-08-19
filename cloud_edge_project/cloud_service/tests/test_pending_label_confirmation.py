from __future__ import annotations

from pathlib import Path

import pytest

from cloud_service.model_update.label_confirmation import (
    CloudReferenceProvider,
    LabelConfirmationResolver,
)
from cloud_service.model_update.service import ModelUpdateService
from cloud_service.storage.database import connect, initialize_database


class StaticTrainingDataSource:
    def __init__(self, samples: list[dict]):
        self._samples = samples

    def load(self, update):
        return self._samples


def _sample(packet_id: str, cloud_label: str = "fault") -> dict:
    return {
        "sample_id": packet_id,
        "packet_id": packet_id,
        "task_id": "task_a",
        "features": {"vibration": {"rms": 1.0}},
        "historical_edge_result": {
            "label": "normal",
            "risk_level": "normal",
            "version": "edge_v1",
        },
        "cloud_label": cloud_label,
        "is_cloud_reviewed": True,
        "sample_pools": ["focus"],
    }


class AlwaysCloudReferenceProvider(CloudReferenceProvider):
    def confirm(self, sample):
        return {
            "packet_id": sample.get("packet_id"),
            "confirmed_label": sample.get("cloud_label") or "normal",
            "label_source": "cloud_reference",
        }


class AuthoritativeFor(LabelConfirmationResolver):
    def __init__(self, ground_truth: dict[str, str], human: dict[str, str]):
        self._ground_truth = ground_truth
        self._human = human

    def confirm(self, sample):
        return None

    def confirm_sources(self, sample):
        packet_id = sample["packet_id"]
        sources = {"cloud_reference": {"packet_id": packet_id, "confirmed_label": sample.get("cloud_label") or "normal", "label_source": "cloud_reference"}}
        if packet_id in self._ground_truth:
            sources["dataset_ground_truth"] = {"packet_id": packet_id, "confirmed_label": self._ground_truth[packet_id], "label_source": "dataset_ground_truth"}
        if packet_id in self._human:
            sources["human_confirmed"] = {"packet_id": packet_id, "confirmed_label": self._human[packet_id], "label_source": "human_confirmed"}
        return sources


def _save_analysis(database_path: Path) -> None:
    result = {
        "analysis_id": "analysis_pending",
        "scenario_type": "bearing",
        "subject_id": "bearing_01",
        "problem_candidates": [
            {
                "problem_id": "problem_pending",
                "problem_layer": "device_arbitration",
                "problem_type": "high_conflict_rate_model",
                "severity": "medium",
                "persistence": "persistent",
                "evidence": {"sample_count": 30},
                "suggested_action": "model_update",
            }
        ],
    }
    import json

    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO global_analysis_result(
                   analysis_id,scenario_type,subject_id,task_count,
                   reviewed_packet_count,cloud_correction_rate,result_json,created_at_ns
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                result["analysis_id"], result["scenario_type"], result["subject_id"],
                20, 20, 0.20, json.dumps(result), 1,
            ),
        )


def test_pending_queue_lists_only_unconfirmed_samples(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    _save_analysis(database_path)
    samples = [
        _sample("p_plain"),
        _sample("p_ground_truth", cloud_label="normal"),
        _sample("p_human"),
    ]
    provider = AuthoritativeFor(
        ground_truth={"p_ground_truth": "normal"}, human={"p_human": "fault"}
    )
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(samples),
        label_provider=provider,
    )
    update = service.create(
        {
            "analysis_id": "analysis_pending",
            "problem_id": "problem_pending",
            "baseline_version": "edge_v1",
        }
    )["update"]

    queue = service.list_pending_human_confirmation(update["update_id"])

    packet_ids = [item["packet_id"] for item in queue["items"]]
    assert packet_ids == ["p_plain"]
    assert queue["pending_count"] == 1
    assert queue["items"][0]["edge_label"] == "normal"
    assert queue["items"][0]["cloud_label"] == "fault"


def test_pending_queue_empty_when_all_authoritative(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    _save_analysis(database_path)
    samples = [_sample("p_gt"), _sample("p_hm")]
    provider = AuthoritativeFor(ground_truth={"p_gt": "normal"}, human={"p_hm": "fault"})
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path,
        training_data_source=StaticTrainingDataSource(samples),
        label_provider=provider,
    )
    update = service.create(
        {
            "analysis_id": "analysis_pending",
            "problem_id": "problem_pending",
            "baseline_version": "edge_v1",
        }
    )["update"]

    queue = service.list_pending_human_confirmation(update["update_id"])

    assert queue["pending_count"] == 0
    assert queue["items"] == []


def test_resolver_reports_all_sources() -> None:
    resolver = LabelConfirmationResolver(
        [
            CloudReferenceProvider(),
            AlwaysCloudReferenceProvider(),
        ]
    )
    sources = resolver.confirm_sources(
        {"packet_id": "packet_x", "cloud_label": "fault"}
    )

    assert sources["cloud_reference"]["confirmed_label"] == "fault"
    assert len(sources) == 1