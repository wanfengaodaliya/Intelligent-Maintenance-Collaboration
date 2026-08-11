from pathlib import Path

from cloud_service.model_update.contracts import ModelUpdateConfig
from cloud_service.model_update.dataset_builder import DatasetBuilder
from cloud_service.model_update.dataset_repository import PacketSourceRepository
from cloud_service.model_update.label_confirmation import LabelConfirmationResolver
from cloud_service.storage.database import connect, initialize_database
from scenarios.bearing.cloud.model_update.dataset_label_provider import (
    DatasetLabelProvider,
)
from scenarios.bearing.cloud.model_update.training_data_source import (
    BearingTrainingDataSource,
)


def test_dataset_label_uses_source_mapping_instead_of_guessing_packet_id(
    tmp_path: Path,
):
    repository = PacketSourceRepository(tmp_path / "source.db")
    repository.save(
        {
            "packet_id": "packet_KI99_misleading",
            "task_id": "task_001",
            "bearing_id": "bearing_01",
            "dataset_name": "paderborn",
            "dataset_version": "paderborn_v1",
            "source_file": "N09_M07_F10_KA01_1.mat",
            "source_bearing_code": "KA01",
            "start_index": 0,
            "end_index": 3200,
            "window_index": 0,
        }
    )
    provider = DatasetLabelProvider(
        repository, {"KA01": "outer_ring_damage", "KI99": "inner_ring_damage"}
    )

    assert provider.confirm({"packet_id": "packet_KI99_misleading"}) == {
        "packet_id": "packet_KI99_misleading",
        "confirmed_label": "outer_ring_damage",
        "label_source": "dataset_ground_truth",
    }


def test_dataset_mapping_can_freeze_fault_label_with_ordered_risk_level(
    tmp_path: Path,
):
    repository = PacketSourceRepository(tmp_path / "source.db")
    repository.save(
        {
            "packet_id": "packet_001",
            "task_id": "task_001",
            "bearing_id": "bearing_01",
            "dataset_name": "paderborn",
            "dataset_version": "paderborn_v1",
            "source_file": "N09_M07_F10_KA01_1.mat",
            "source_bearing_code": "KA01",
            "start_index": 0,
            "end_index": 3200,
            "window_index": 0,
        }
    )
    provider = DatasetLabelProvider(
        repository,
        {
            "KA01": {
                "confirmed_label": "outer_ring_damage",
                "confirmed_risk_level": "abnormal",
            }
        },
    )

    confirmation = provider.confirm({"packet_id": "packet_001"})

    assert confirmation["confirmed_label"] == "outer_ring_damage"
    assert confirmation["confirmed_risk_level"] == "abnormal"


def test_dataset_label_provider_accepts_paderborn_healthy_code(tmp_path: Path):
    repository = PacketSourceRepository(tmp_path / "source.db")
    repository.save(
        {
            "packet_id": "packet_healthy",
            "task_id": "task_healthy",
            "bearing_id": "bearing_01",
            "dataset_name": "paderborn",
            "dataset_version": "paderborn_v1",
            "source_file": "N09_M07_F10_K001_1.mat",
            "source_bearing_code": "K001",
            "start_index": 0,
            "end_index": 3200,
            "window_index": 0,
        }
    )
    provider = DatasetLabelProvider(repository, {"K001": "healthy"})

    assert provider.confirm({"packet_id": "packet_healthy"}) == {
        "packet_id": "packet_healthy",
        "confirmed_label": "healthy",
        "label_source": "dataset_ground_truth",
    }


def test_cloud_reference_is_used_only_when_no_final_confirmation_exists():
    resolver = LabelConfirmationResolver(
        [
            lambda sample: None,
            lambda sample: {
                "packet_id": sample["packet_id"],
                "confirmed_label": sample.get("cloud_label"),
                "label_source": "cloud_reference",
            }
            if sample.get("cloud_label")
            else None,
        ]
    )

    assert resolver.confirm(
        {"packet_id": "packet_001", "cloud_label": "abnormal"}
    )["label_source"] == "cloud_reference"
    assert resolver.confirm({"packet_id": "packet_002"}) is None


def _sample(index: int, group: str, *, agreement: bool = False):
    label = "normal" if agreement else "abnormal"
    return {
        "sample_id": f"sample_{index}",
        "packet_id": f"packet_{index}",
        "task_id": f"task_{group}",
        "source_file": f"{group}.mat",
        "features": {"vibration": {"rms": float(index), "kurtosis": 3.0}},
        "historical_edge_result": {"label": "normal"},
        "cloud_label": label,
        "is_cloud_reviewed": True,
    }


def test_group_split_prevents_leakage_and_keeps_agreement_focus_sample():
    samples = [
        _sample(index, group, agreement=index == 1)
        for index, group in enumerate(("a", "a", "b", "b", "c", "c"), 1)
    ]
    resolver = LabelConfirmationResolver(
        [
            lambda sample: {
                "packet_id": sample["packet_id"],
                "confirmed_label": sample["cloud_label"],
                "label_source": "cloud_reference",
            }
        ]
    )

    manifest = DatasetBuilder(ModelUpdateConfig()).build(
        update={
            "update_id": "update_001",
            "baseline_version": "edge_v1",
            "problem_type": "risk_underestimation",
            "problem_context": {"operating_condition": "high_load"},
        },
        samples=samples,
        label_provider=resolver,
        feature_pipeline_version="edge_feature_v1",
    )

    partition_by_sample = {
        sample_id: partition
        for partition, sample_ids in (
            ("train", manifest["train_sample_ids"]),
            ("validation", manifest["validation_sample_ids"]),
            ("test", manifest["test_sample_ids"]),
        )
        for sample_id in sample_ids
    }
    for left, right in ((1, 2), (3, 4), (5, 6)):
        assert partition_by_sample[f"sample_{left}"] == partition_by_sample[
            f"sample_{right}"
        ]
    assert "sample_1" in manifest["focus_sample_ids"]
    assert manifest["label_source_summary"] == {"cloud_reference": 6}


def test_group_split_links_samples_sharing_task_even_across_source_files():
    samples = [
        {**_sample(1, "source_a"), "task_id": "shared_task"},
        {**_sample(2, "source_b"), "task_id": "shared_task"},
        _sample(3, "source_c"),
        _sample(4, "source_d"),
    ]
    resolver = LabelConfirmationResolver(
        [
            lambda sample: {
                "packet_id": sample["packet_id"],
                "confirmed_label": sample["cloud_label"],
                "label_source": "cloud_reference",
            }
        ]
    )

    manifest = DatasetBuilder(ModelUpdateConfig()).build(
        update={
            "update_id": "update_001",
            "baseline_version": "edge_v1",
            "problem_type": "risk_underestimation",
            "problem_context": {},
        },
        samples=samples,
        label_provider=resolver,
        feature_pipeline_version="edge_feature_v1",
    )

    assert manifest["sample_group_keys"]["sample_1"] == manifest[
        "sample_group_keys"
    ]["sample_2"]


def test_frozen_test_contains_focus_sample_for_target_validation():
    samples = [_sample(index, group) for index, group in enumerate(("a", "b", "c", "d"), 1)]
    for sample in samples:
        sample["is_cloud_reviewed"] = sample["source_file"] == "b.mat"
    resolver = LabelConfirmationResolver(
        [
            lambda sample: {
                "packet_id": sample["packet_id"],
                "confirmed_label": sample["cloud_label"],
                "label_source": "cloud_reference",
            }
        ]
    )

    manifest = DatasetBuilder(ModelUpdateConfig()).build(
        update={
            "update_id": "update_001",
            "baseline_version": "edge_v1",
            "problem_type": "risk_underestimation",
            "problem_context": {},
        },
        samples=samples,
        label_provider=resolver,
        feature_pipeline_version="edge_feature_v1",
    )

    assert set(manifest["focus_sample_ids"]) & set(manifest["test_sample_ids"])


def test_samples_without_any_label_are_excluded_from_supervised_manifest():
    resolver = LabelConfirmationResolver([lambda sample: None])
    samples = [_sample(index, group) for index, group in enumerate(("a", "b", "c"), 1)]

    try:
        DatasetBuilder(ModelUpdateConfig()).build(
            update={
                "update_id": "update_001",
                "baseline_version": "edge_v1",
                "problem_type": "risk_underestimation",
                "problem_context": {},
            },
            samples=samples,
            label_provider=resolver,
            feature_pipeline_version="edge_feature_v1",
        )
    except ValueError as error:
        assert str(error) == "SUPERVISED_SAMPLES_NOT_FOUND"
    else:
        raise AssertionError("unlabeled samples must not enter the manifest")


def test_default_training_source_reads_history_and_marks_reviewed_focus(tmp_path: Path):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    source_repository = PacketSourceRepository(database_path)
    for index in (1, 2):
        source_repository.save(
            {
                "packet_id": f"packet_{index}",
                "task_id": f"task_{index}",
                "bearing_id": "bearing_01",
                "dataset_name": "paderborn",
                "dataset_version": "paderborn_v1",
                "source_file": f"N09_M07_F10_KA0{index}_{index}.mat",
                "source_bearing_code": f"KA0{index}",
                "start_index": 0,
                "end_index": 3200,
                "window_index": 0,
            }
        )
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO senders(sender_id,created_at_ns,updated_at_ns,device_id,bearing_id) VALUES (?,?,?,?,?)",
            ("sender_01", 1, 1, "machine_01", "bearing_01"),
        )
        for index in (1, 2):
            connection.execute(
                """INSERT INTO edge_packet_summary(
                       sender_id,packet_id,device_id,task_id,bearing_id,
                       sequence_number,edge_node_id,end_timestamp_ns,received_at_ns,
                       processing_status,edge_result,confidence,edge_risk_level,
                       edge_model_version,vibration_rms,vibration_kurtosis,
                       summary_json,payload_sha256
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "sender_01", f"packet_{index}", "machine_01",
                    f"task_{index}", "bearing_01", index, "edge_01", index,
                    index, "perception_completed", "normal", 0.8, "low",
                    "edge_v1", 1.0, 3.0, "{}", f"sha_{index}",
                ),
            )
        connection.execute(
            """INSERT INTO cloud_review(
                   review_id,sender_id,anchor_packet_id,device_id,task_id,bearing_id,
                   feature_extractor_version,schema_version,review_status,context_status,
                   data_quality_valid,data_quality_json,created_at_ns,updated_at_ns
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "review_01", "sender_01", "packet_1", "machine_01", "task_1",
                "bearing_01", "edge_feature_v1", "1.0", "complete",
                "not_requested", 1, "{}", 1, 1,
            ),
        )
        connection.execute(
            """INSERT INTO final_diagnosis_summary(
                   review_id,status,backend,model_name,summary_json,created_at_ns,updated_at_ns
               ) VALUES (?,?,?,?,?,?,?)""",
            ("review_01", "succeeded", "mock", "mock", '{"label":"abnormal"}', 1, 1),
        )

    samples = BearingTrainingDataSource(
        database_path, source_repository
    ).load({"subject_id": "machine_01", "baseline_version": "edge_v1"})

    assert [sample["packet_id"] for sample in samples] == ["packet_1", "packet_2"]
    assert samples[0]["is_cloud_reviewed"] is True
    assert samples[1]["is_cloud_reviewed"] is False
    assert "cloud_enhanced_features" not in samples[0]["features"]
