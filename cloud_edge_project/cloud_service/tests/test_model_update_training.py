"""End-to-end tests for the real offline trainers (edge H5 + cloud MOMENT)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cloud_service.model_update.candidate_registry import CandidateRegistry
from cloud_service.model_update.contracts import ModelUpdateConfig
from cloud_service.model_update.dataset_builder import DatasetBuilder
from cloud_service.model_update.dataset_repository import PacketSourceRepository
from cloud_service.model_update.service import ModelUpdateError, ModelUpdateService
from cloud_service.model_update.training import (
    CloudMomentTrainer,
    EdgeH5Trainer,
    OfflineTrainingRunner,
    RawTrainingSampleLoader,
    compute_physical_features,
)
from cloud_service.storage.database import initialize_database

SAMPLE_RATE = 64_000
WINDOW = 3_200
WINDOWS_PER_FILE = 3

GROUP_LABELS = {
    "g1": "healthy",
    "g2": "outer_ring_damage",
    "g3": "inner_ring_damage",
    "g4": "healthy",
    "g5": "outer_ring_damage",
    "g6": "inner_ring_damage",
}


def _class_signal(label: str, length: int = WINDOW * WINDOWS_PER_FILE) -> np.ndarray:
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(length) * 0.05
    if label == "healthy":
        return (noise * 0.5).astype(np.float64)
    period = 640 if label == "outer_ring_damage" else 960
    impulses = np.zeros(length)
    for start in range(0, length, period):
        end = min(start + 40, length)
        impulses[start:end] = np.exp(-np.arange(end - start) / 12.0)
    return (impulses * 2.0 + noise).astype(np.float64)


def _condition_channels(length: int, base: float) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(1)
    return {
        "speed": np.full(length, 1000.0 + base * 10.0) + rng.standard_normal(length) * 0.5,
        "torque": np.full(length, 5.0 + base * 0.1) + rng.standard_normal(length) * 0.01,
        "force": np.full(length, 500.0 + base * 5.0) + rng.standard_normal(length) * 0.5,
        "temp_2_bearing_module": np.full(length, 40.0 + base * 0.5),
    }


def _write_mat(path: Path, stem: str, label: str, base: float) -> None:
    import scipy.io as sio

    length = WINDOW * WINDOWS_PER_FILE
    channels = [{"Name": "vibration_1", "Data": _class_signal(label, length)}]
    for name, data in _condition_channels(length, base).items():
        channels.append({"Name": name, "Data": data})
    sio.savemat(str(path), {stem: {"Y": channels}})


def _cloud_reference_resolver():
    from cloud_service.model_update.label_confirmation import (
        LabelConfirmationResolver,
    )

    return LabelConfirmationResolver(
        [
            lambda sample: {
                "packet_id": sample["packet_id"],
                "confirmed_label": sample["cloud_label"],
                "label_source": "cloud_reference",
            }
        ]
    )


def _write_synthetic_source(
    tmp_path: Path, database_path: Path
) -> tuple[list[dict[str, object]], Path]:
    """Write Paderborn-format .mat files + packet source mappings.

    Returns the sample descriptors and the raw data root.
    """

    data_root = tmp_path / "paderborn"
    source_repository = PacketSourceRepository(database_path)
    samples: list[dict[str, object]] = []
    for group_index, (group, label) in enumerate(GROUP_LABELS.items(), 1):
        bearing_code = f"K{group.upper()}"
        source_file = f"{group}.mat"
        mat_dir = data_root / bearing_code
        mat_dir.mkdir(parents=True, exist_ok=True)
        _write_mat(mat_dir / source_file, group, label, base=float(group_index))
        for window_index in range(WINDOWS_PER_FILE):
            packet_id = f"packet_{group}_{window_index}"
            source_repository.save(
                {
                    "packet_id": packet_id,
                    "task_id": f"task_{group}",
                    "bearing_id": "bearing_01",
                    "dataset_name": "paderborn",
                    "dataset_version": "paderborn_v1",
                    "source_file": source_file,
                    "source_bearing_code": bearing_code,
                    "start_index": window_index * WINDOW,
                    "end_index": (window_index + 1) * WINDOW,
                    "window_index": window_index,
                }
            )
            samples.append(
                {
                    "sample_id": packet_id,
                    "packet_id": packet_id,
                    "task_id": f"task_{group}",
                    "source_file": source_file,
                    "features": {"vibration": {"rms": 1.0, "kurtosis": 3.0}},
                    "historical_edge_result": {
                        "label": "normal",
                        "risk_level": "low",
                        "version": "edge_v1",
                    },
                    "cloud_label": label,
                    "is_cloud_reviewed": window_index == 0,
                }
            )
    return samples, data_root


def _build_manifest(
    samples: list[dict[str, object]], update_id: str = "update_synthetic"
) -> dict[str, object]:
    return DatasetBuilder(ModelUpdateConfig()).build(
        update={
            "update_id": update_id,
            "baseline_version": "edge_v1",
            "problem_type": "risk_underestimation",
            "problem_context": {},
        },
        samples=samples,
        label_provider=_cloud_reference_resolver(),
        feature_pipeline_version="edge_feature_v1",
    )


def _write_baseline_checkpoint(dir_path: Path) -> Path:
    import torch

    from cloud_service.model_update.training import PhysicalFusionModel

    dir_path.mkdir(parents=True, exist_ok=True)
    model = PhysicalFusionModel(num_classes=3, phys_dim=19, cond_dim=13)
    state = {f"h5.{key}": value for key, value in model.state_dict().items()}
    checkpoint = dir_path / "best_model.pt"
    torch.save({"model_state_dict": state}, str(checkpoint))
    (dir_path / "physical_feature_normalization.json").write_text(
        json.dumps({"mean": np.zeros(19).tolist(), "std": np.ones(19).tolist()}),
        encoding="utf-8",
    )
    (dir_path / "condition_norm.json").write_text(
        json.dumps({"mean": np.zeros(13).tolist(), "std": np.ones(13).tolist()}),
        encoding="utf-8",
    )
    return checkpoint


def _edge_trainer(
    tmp_path: Path, database_path: Path, *, config: dict[str, object] | None = None
) -> EdgeH5Trainer:
    _, data_root = _write_synthetic_source(tmp_path, database_path)
    baseline_dir = tmp_path / "baseline"
    baseline_checkpoint = _write_baseline_checkpoint(baseline_dir)
    loader = RawTrainingSampleLoader(PacketSourceRepository(database_path), data_root)
    return EdgeH5Trainer(
        loader=loader,
        baseline_checkpoint=baseline_checkpoint,
        baseline_dir=baseline_dir,
        config={"max_epochs": 3, "patience": 2, "batch_size": 64, **(config or {})},
    )


class _FakeMomentModel:
    def __init__(self) -> None:
        import torch.nn as nn

        self.net = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(1, 3),
        )
        self.training = True

    def forward(self, vibration, condition):
        return self.net(vibration)

    def __call__(self, vibration, condition):
        return self.forward(vibration, condition)

    def state_dict(self):
        return self.net.state_dict()

    def load_state_dict(self, state):
        self.net.load_state_dict(state)

    def to(self, device):
        self.net.to(device)
        return self

    def train(self, mode: bool = True):
        self.training = mode
        self.net.train(mode)
        return self

    def eval(self):
        return self.train(False)

    def parameters(self):
        return self.net.parameters()


def _cloud_trainer(
    tmp_path: Path, database_path: Path, *, config: dict[str, object] | None = None
) -> CloudMomentTrainer:
    _, data_root = _write_synthetic_source(tmp_path, database_path)
    baseline_dir = tmp_path / "cloud_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_checkpoint = baseline_dir / "best_model.pt"
    import torch

    torch.save(
        {"model_state_dict": _FakeMomentModel().state_dict()}, str(baseline_checkpoint)
    )
    loader = RawTrainingSampleLoader(PacketSourceRepository(database_path), data_root)
    return CloudMomentTrainer(
        loader=loader,
        baseline_checkpoint=baseline_checkpoint,
        pretrained_path=tmp_path / "pretrained",
        model_module_path=tmp_path / "moment_model.py",
        model_factory=_FakeMomentModel,
        config={"max_epochs": 2, "patience": 2, "batch_size": 32, **(config or {})},
    )


def test_compute_physical_features_returns_19d_vector() -> None:
    features = compute_physical_features(_class_signal("healthy", WINDOW))
    assert features.shape == (19,)
    assert np.isfinite(features).all()


def test_edge_h5_trainer_produces_loadable_bundle(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    manifest = _build_manifest(samples)
    trainer = _edge_trainer(tmp_path, database_path)
    output_dir = tmp_path / "out" / "edge"

    payload = trainer.train(manifest, output_dir)

    assert payload["model_type"] == "distilled_h5"
    assert payload["candidate_version"].startswith("distilled_h5_kd_")
    assert payload["training_dataset_id"] == manifest["dataset_id"]
    assert payload["training_metrics"]["train_sample_count"] > 0
    assert payload["training_metrics"]["val_sample_count"] > 0
    for name in (
        "best_model.pt",
        "checkpoint_sha256.txt",
        "physical_feature_normalization.json",
        "condition_norm.json",
        "manifest.json",
        "training_history.csv",
        "model_metadata.json",
    ):
        assert (output_dir / name).is_file(), name
    assert (
        output_dir / "checkpoint_sha256.txt"
    ).read_text(encoding="utf-8").strip() == payload["artifact_sha256"]

    candidate = CandidateRegistry(tmp_path / "registry").register(manifest, payload)
    assert candidate["candidate_version"] == payload["candidate_version"]

    import torch

    from cloud_service.model_update.training import PhysicalFusionModel

    model = PhysicalFusionModel(num_classes=3, phys_dim=19, cond_dim=13)
    checkpoint = torch.load(
        str(output_dir / "best_model.pt"), map_location="cpu", weights_only=False
    )
    state = {
        key[3:]: value
        for key, value in checkpoint["model_state_dict"].items()
        if key.startswith("h5.")
    }
    model.load_state_dict(state)
    model.eval()


def test_offline_training_runner_dispatches_edge(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    manifest = _build_manifest(samples)
    runner = OfflineTrainingRunner(
        edge_trainer=_edge_trainer(tmp_path, database_path),
        cloud_trainer=_cloud_trainer(tmp_path, database_path),
    )

    payload = runner.run(
        {"model_type": "distilled_h5", "output_dir": str(tmp_path / "out" / "edge")},
        manifest,
    )

    assert payload["model_type"] == "distilled_h5"
    assert (tmp_path / "out" / "edge" / "best_model.pt").is_file()


def test_offline_training_runner_dispatches_cloud(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    manifest = _build_manifest(samples)
    runner = OfflineTrainingRunner(
        edge_trainer=_edge_trainer(tmp_path, database_path),
        cloud_trainer=_cloud_trainer(tmp_path, database_path),
    )

    payload = runner.run(
        {
            "model_type": "moment_light_adapt",
            "output_dir": str(tmp_path / "out" / "cloud"),
        },
        manifest,
    )

    assert payload["model_type"] == "moment_light_adapt"
    assert payload["candidate_version"].startswith("moment_light_adapt_kd_")
    assert (tmp_path / "out" / "cloud" / "best_model.pt").is_file()
    assert (tmp_path / "out" / "cloud" / "condition_norm.json").is_file()


def test_runner_rejects_unknown_model_type(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    manifest = _build_manifest(samples)
    runner = OfflineTrainingRunner(
        edge_trainer=_edge_trainer(tmp_path, database_path),
        cloud_trainer=_cloud_trainer(tmp_path, database_path),
    )

    with pytest.raises(ValueError, match="UNSUPPORTED_MODEL_TYPE"):
        runner.run({"model_type": "generic", "output_dir": str(tmp_path)}, manifest)


def _save_analysis(database_path: Path) -> None:
    from cloud_service.storage.database import connect

    result = {
        "analysis_id": "analysis_training",
        "scenario_type": "bearing",
        "subject_id": "bearing_01",
        "problem_candidates": [
            {
                "problem_id": "problem_training",
                "problem_layer": "device_arbitration",
                "problem_type": "high_conflict_rate_model",
                "severity": "medium",
                "persistence": "persistent",
                "evidence": {"sample_count": 30},
                "suggested_action": "model_update",
            }
        ],
    }
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


class StaticTrainingDataSource:
    def __init__(self, samples: list[dict[str, object]]) -> None:
        self.samples = samples

    def load(self, update):
        return self.samples


def test_run_training_lifecycle_via_service(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    _save_analysis(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    runner = OfflineTrainingRunner(
        edge_trainer=_edge_trainer(tmp_path, database_path),
        cloud_trainer=_cloud_trainer(tmp_path, database_path),
    )
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path / "updates",
        packet_source_database_path=database_path,
        training_data_source=StaticTrainingDataSource(samples),
        label_provider=_cloud_reference_resolver(),
        trainer=runner,
    )
    update = service.create(
        {
            "analysis_id": "analysis_training",
            "problem_id": "problem_training",
            "baseline_version": "edge_v1",
            "model_type": "distilled_h5",
        }
    )["update"]

    prepared = service.prepare_data(update["update_id"])
    assert prepared["status"] == "waiting_training"
    training = service.start_training(update["update_id"])
    assert training["status"] == "training"
    trained = service.run_training(update["update_id"])

    assert trained["status"] == "trained"
    assert trained["candidate_version"].startswith("distilled_h5_kd_")
    artifact = trained["candidate_artifact"]
    assert artifact["artifact_path"]
    assert artifact["model_type"] == "distilled_h5"
    download = service.get_download_artifact(update["update_id"])
    assert download["candidate_version"] == trained["candidate_version"]
    assert download["artifact_bundle"]["entries"]


def test_run_training_requires_trainer_and_plan(tmp_path: Path) -> None:
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    _save_analysis(database_path)
    samples, _ = _write_synthetic_source(tmp_path, database_path)
    service = ModelUpdateService(
        database_path,
        data_root=tmp_path / "updates",
        packet_source_database_path=database_path,
        training_data_source=StaticTrainingDataSource(samples),
        label_provider=_cloud_reference_resolver(),
        trainer=None,
    )
    update = service.create(
        {
            "analysis_id": "analysis_training",
            "problem_id": "problem_training",
            "baseline_version": "edge_v1",
        }
    )["update"]
    service.prepare_data(update["update_id"])
    service.start_training(update["update_id"])

    with pytest.raises(ModelUpdateError, match="MODEL_UPDATE_SCENARIO_NOT_CONFIGURED"):
        service.run_training(update["update_id"])
