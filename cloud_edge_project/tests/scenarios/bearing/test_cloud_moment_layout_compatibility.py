from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_EXPORTS = (
    (
        "moment_backbone",
        "moment_backbone",
        ("load_moment_backbone",),
    ),
    (
        "moment_light_adapt",
        "moment_light_adapt",
        (
            "LABEL_NAMES",
            "MODEL_VERSION",
            "MomentPrediction",
            "MomentReviewPolicy",
            "build_condition_vector",
            "deployment_workspace_root",
            "MomentLightAdaptRunner",
        ),
    ),
)
EXPECTED_LABELS = ("healthy", "outer_ring_damage", "inner_ring_damage")
EXPECTED_MODEL_VERSION = "moment-scl05-final"


def _operating_context() -> dict[str, object]:
    return {
        "shaft_speed_rpm": {
            "mean": 1,
            "standard_deviation": 2,
            "minimum": 3,
            "maximum": 4,
        },
        "load_torque_nm": {
            "mean": 5,
            "standard_deviation": 6,
            "minimum": 7,
            "maximum": 8,
        },
        "bearing_radial_load_n": {
            "mean": 9,
            "standard_deviation": 10,
            "minimum": 11,
            "maximum": 12,
        },
        "bearing_module_temperature_c": 13,
    }


def test_cloud_moment_target_layout_exists() -> None:
    expected_paths = (
        PROJECT_ROOT
        / "scenarios"
        / "bearing"
        / "cloud_diagnosis"
        / "moment_backbone.py",
        PROJECT_ROOT
        / "scenarios"
        / "bearing"
        / "cloud_diagnosis"
        / "moment_light_adapt.py",
        PROJECT_ROOT
        / "compatibility"
        / "bearing_v12"
        / "cloud_moment_exports.py",
    )

    assert all(path.is_file() for path in expected_paths)


@pytest.mark.parametrize(
    ("scenario_module_name", "legacy_module_name", "public_names"),
    MODULE_EXPORTS,
)
def test_legacy_cloud_moment_exports_are_scenario_objects(
    scenario_module_name: str,
    legacy_module_name: str,
    public_names: tuple[str, ...],
) -> None:
    scenario_module = importlib.import_module(
        f"scenarios.bearing.cloud_diagnosis.{scenario_module_name}"
    )
    compatibility_module = importlib.import_module(
        "compatibility.bearing_v12.cloud_moment_exports"
    )
    legacy_module = importlib.import_module(f"cloud_service.{legacy_module_name}")

    assert tuple(legacy_module.__all__) == public_names
    assert set(public_names).issubset(compatibility_module.__all__)
    for public_name in public_names:
        scenario_value = getattr(scenario_module, public_name)
        assert getattr(compatibility_module, public_name) is scenario_value
        assert getattr(legacy_module, public_name) is scenario_value


def test_cloud_moment_compatibility_exports_have_exact_public_surface() -> None:
    compatibility_module = importlib.import_module(
        "compatibility.bearing_v12.cloud_moment_exports"
    )
    expected = tuple(
        public_name
        for _scenario_name, _legacy_name, public_names in MODULE_EXPORTS
        for public_name in public_names
    )

    assert tuple(compatibility_module.__all__) == expected


def test_cloud_service_receives_scenario_runtime_through_legacy_boundary() -> None:
    service_module = importlib.import_module("cloud_service.service")
    scenario_module = importlib.import_module(
        "scenarios.bearing.cloud_diagnosis.moment_light_adapt"
    )

    assert service_module.MomentLightAdaptRunner is (
        scenario_module.MomentLightAdaptRunner
    )
    assert service_module.MomentReviewPolicy is scenario_module.MomentReviewPolicy
    assert service_module.MODEL_VERSION is scenario_module.MODEL_VERSION


def test_legacy_condition_vector_and_policy_match_frozen_goldens() -> None:
    from cloud_service.moment_light_adapt import (
        LABEL_NAMES,
        MODEL_VERSION,
        MomentReviewPolicy,
        build_condition_vector,
    )

    vector = build_condition_vector(_operating_context())

    assert LABEL_NAMES == EXPECTED_LABELS
    assert MODEL_VERSION == EXPECTED_MODEL_VERSION
    np.testing.assert_array_equal(vector, np.arange(1, 14, dtype=np.float32))
    assert vector.shape == (13,)
    assert vector.dtype == np.float32
    policy = MomentReviewPolicy()
    assert policy.decide("healthy") == (
        "normal",
        "low",
        "continue_operation",
    )
    assert policy.decide("outer_ring_damage") == (
        "warning",
        "medium",
        "scheduled_inspection",
    )
    assert policy.decide("inner_ring_damage") == (
        "warning",
        "medium",
        "scheduled_inspection",
    )
    with pytest.raises(KeyError, match="^'unsupported'$"):
        policy.decide("unsupported")
    with pytest.raises(KeyError, match="^'shaft_speed_rpm'$"):
        build_condition_vector({})


def test_legacy_backbone_loader_matches_frozen_contract() -> None:
    from cloud_service.moment_backbone import load_moment_backbone

    class FakeModel:
        initialized = False

        def init(self) -> None:
            warnings.warn(
                "Only reconstruction head is pre-trained for this fake",
                UserWarning,
            )
            self.initialized = True

    class FakePipeline:
        call: tuple[str, dict[str, object]] | None = None
        model = FakeModel()

        @classmethod
        def from_pretrained(
            cls,
            pretrained_path: str,
            model_kwargs: dict[str, object],
        ) -> FakeModel:
            cls.call = (pretrained_path, model_kwargs)
            return cls.model

    with warnings.catch_warnings(record=True) as caught:
        loaded = load_moment_backbone(
            "P:/moment",
            3,
            pipeline_class=FakePipeline,
        )

    assert FakePipeline.call == (
        "P:/moment",
        {
            "task_name": "classification",
            "n_channels": 1,
            "num_class": 3,
        },
    )
    assert loaded is FakePipeline.model
    assert loaded.initialized is True
    assert caught == []

    class BackboneFailure(RuntimeError):
        pass

    class RaisingPipeline:
        @classmethod
        def from_pretrained(
            cls,
            pretrained_path: str,
            model_kwargs: dict[str, object],
        ) -> object:
            raise BackboneFailure(f"cannot load {pretrained_path}")

    with pytest.raises(BackboneFailure, match="^cannot load P:/broken$"):
        load_moment_backbone(
            "P:/broken",
            3,
            pipeline_class=RaisingPipeline,
        )


def test_legacy_workspace_resolution_matches_frozen_contract(tmp_path: Path) -> None:
    from cloud_service.moment_light_adapt import deployment_workspace_root

    workspace = tmp_path / "workspace"
    (workspace / "experiments").mkdir(parents=True)
    pretrained = workspace / "models" / "moment"
    pretrained.mkdir(parents=True)

    assert deployment_workspace_root(pretrained) == workspace
    assert deployment_workspace_root(tmp_path / "without" / "experiments") == (
        PROJECT_ROOT
    )


class _Cuda:
    @staticmethod
    def is_available() -> bool:
        return True


class _NoGrad:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> bool:
        return False


class _Tensor:
    def __init__(self, values: object):
        self.values = np.asarray(values)

    def to(self, _device: object) -> _Tensor:
        return self

    def __getitem__(self, item: object) -> _Tensor:
        return _Tensor(self.values[item])

    def detach(self) -> _Tensor:
        return self

    def cpu(self) -> _Tensor:
        return self

    def numpy(self) -> np.ndarray:
        return self.values


class _Torch:
    cuda = _Cuda()

    @staticmethod
    def device(value: str) -> str:
        return value

    @staticmethod
    def from_numpy(values: np.ndarray) -> _Tensor:
        return _Tensor(values)

    @staticmethod
    def no_grad() -> _NoGrad:
        return _NoGrad()

    @staticmethod
    def softmax(tensor: _Tensor, dim: int) -> _Tensor:
        shifted = tensor.values - np.max(tensor.values, axis=dim, keepdims=True)
        exponentials = np.exp(shifted)
        return _Tensor(
            exponentials / np.sum(exponentials, axis=dim, keepdims=True)
        )


class _Model:
    def __call__(self, raw: _Tensor, condition: _Tensor) -> _Tensor:
        assert raw.values.shape == (1, 4)
        np.testing.assert_array_equal(
            condition.values,
            np.arange(1, 14, dtype=np.float32).reshape(1, 13),
        )
        return _Tensor([[1.0, 2.0, 0.0]])


def test_legacy_runner_state_device_and_prediction_match_frozen_goldens() -> None:
    from cloud_service.moment_light_adapt import MomentLightAdaptRunner

    settings = SimpleNamespace(moment_device="auto")
    runner = MomentLightAdaptRunner(settings)

    assert runner.loaded is False
    assert runner.model_version == EXPECTED_MODEL_VERSION
    assert runner.gpu_available is False
    with pytest.raises(
        RuntimeError,
        match="^MOMENT LIGHT_ADAPT runner is not loaded$",
    ):
        runner.predict({"values": [0.0]}, _operating_context())

    runner._torch = _Torch()
    runner._device = runner._resolve_device(runner._torch)
    runner._model = _Model()
    runner._condition_mean = np.zeros(13, dtype=np.float32)
    runner._condition_std = np.ones(13, dtype=np.float32)

    prediction = runner.predict(
        {"values": [1.0, 2.0, 3.0, 4.0]},
        _operating_context(),
    )

    assert runner._device == "cuda"
    assert runner.gpu_available is True
    assert prediction.label == "outer_ring_damage"
    assert prediction.confidence == pytest.approx(0.6652409557748218)
    assert prediction.probabilities == pytest.approx(
        {
            "healthy": 0.24472847105479764,
            "outer_ring_damage": 0.6652409557748218,
            "inner_ring_damage": 0.09003057317038046,
        }
    )
    assert prediction.model_version == EXPECTED_MODEL_VERSION


class _LoadCuda:
    @staticmethod
    def is_available() -> bool:
        return False


class _LoadTorch:
    cuda = _LoadCuda()

    def __init__(self) -> None:
        self.load_calls: list[tuple[Path, object]] = []

    @staticmethod
    def device(value: str) -> str:
        return value

    def load(self, path: Path, *, map_location: object) -> dict[str, object]:
        self.load_calls.append((path, map_location))
        return {"model_state_dict": {"weight": [1.0, 2.0]}}


def test_runner_loads_dynamic_model_and_normalization_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scenarios.bearing.cloud_diagnosis.moment_backbone import (
        load_moment_backbone,
    )
    from scenarios.bearing.cloud_diagnosis.moment_light_adapt import (
        MomentLightAdaptRunner,
    )

    workspace = tmp_path / "workspace"
    (workspace / "experiments").mkdir(parents=True)
    pretrained_path = workspace / "pretrained" / "MOMENT-1-small"
    pretrained_path.mkdir(parents=True)
    deployment_dir = workspace / "deployment"
    deployment_dir.mkdir()
    (deployment_dir / "moment_model.py").write_text(
        """
class BuiltModel:
    def __init__(self, pretrained_path, num_classes, condition_dropout):
        self.pretrained_path = pretrained_path
        self.num_classes = num_classes
        self.condition_dropout = condition_dropout
        self.loaded_state = None
        self.device = None
        self.evaluated = False

    def load_state_dict(self, state):
        self.loaded_state = state

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.evaluated = True
        return self

def build_model(pretrained_path, num_classes, condition_dropout):
    return BuiltModel(pretrained_path, num_classes, condition_dropout)
""",
        encoding="utf-8",
    )
    checkpoint_path = workspace / "checkpoint.pt"
    checkpoint_path.write_bytes(b"fake checkpoint")
    condition_norm_path = workspace / "condition_norm.json"
    condition_norm_path.write_text(
        json.dumps(
            {
                "mean": list(range(13)),
                "std": list(range(1, 14)),
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        moment_device="auto",
        moment_pretrained_path=pretrained_path,
        moment_deployment_dir=deployment_dir,
        moment_checkpoint_path=checkpoint_path,
        moment_condition_norm_path=condition_norm_path,
    )
    fake_torch = _LoadTorch()
    adapter_name = "experiments.diagnosis_models.moment.adapter"
    dynamic_name = "_cloud_moment_light_adapt_model"
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.delitem(sys.modules, adapter_name, raising=False)
    monkeypatch.delitem(sys.modules, dynamic_name, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    runner = MomentLightAdaptRunner(settings)

    runner.load()
    loaded_model = runner._model

    assert runner.loaded is True
    assert fake_torch.load_calls == [(checkpoint_path, "cpu")]
    assert loaded_model.__class__.__module__ == dynamic_name
    assert loaded_model.pretrained_path == str(pretrained_path)
    assert loaded_model.num_classes == 3
    assert loaded_model.condition_dropout == 0.0
    assert loaded_model.loaded_state == {"weight": [1.0, 2.0]}
    assert loaded_model.device == "cpu"
    assert loaded_model.evaluated is True
    np.testing.assert_array_equal(
        runner._condition_mean,
        np.arange(13, dtype=np.float32),
    )
    np.testing.assert_array_equal(
        runner._condition_std,
        np.arange(1, 14, dtype=np.float32),
    )
    assert runner._condition_mean.dtype == np.float32
    assert runner._condition_std.dtype == np.float32
    assert sys.modules[adapter_name].load_moment_backbone is load_moment_backbone
    assert sys.modules[dynamic_name].build_model is not None

    runner.load()

    assert runner._model is loaded_model
    assert fake_torch.load_calls == [(checkpoint_path, "cpu")]


def test_runner_model_builder_preserves_exact_missing_loader_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module(
        "scenarios.bearing.cloud_diagnosis.moment_light_adapt"
    )
    deployment_dir = tmp_path / "deployment"
    settings = SimpleNamespace(
        moment_pretrained_path=tmp_path / "pretrained",
        moment_deployment_dir=deployment_dir,
    )
    runner = runtime_module.MomentLightAdaptRunner(settings)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(
        runtime_module.importlib.util,
        "spec_from_file_location",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ImportError) as error:
        runner._load_model_builder()

    assert str(error.value) == (
        f"cannot load MOMENT model definition: {deployment_dir / 'moment_model.py'}"
    )


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT),))
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_submodule_import_keeps_provider_assembly_lazy() -> None:
    code = """
import importlib
import sys

importlib.import_module("cloud_service.moment_backbone")
assert "scenarios.bearing.cloud_diagnosis.provider" not in sys.modules
package = importlib.import_module("scenarios.bearing.cloud_diagnosis")
provider = package.BearingCloudDiagnosisProvider
assert provider.__module__ == "scenarios.bearing.cloud_diagnosis.provider"
assert "scenarios.bearing.cloud_diagnosis.provider" in sys.modules
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "entry_import",
    (
        "cloud_service.moment_backbone",
        "cloud_service.moment_light_adapt",
        "scenarios.bearing.cloud_diagnosis.moment_backbone",
        "scenarios.bearing.cloud_diagnosis.moment_light_adapt",
        "compatibility.bearing_v12.cloud_moment_exports",
    ),
)
def test_cloud_moment_modules_support_cold_import_orders(entry_import: str) -> None:
    code = f"""
import importlib

importlib.import_module({entry_import!r})
compatibility = importlib.import_module(
    "compatibility.bearing_v12.cloud_moment_exports"
)
for scenario_name, legacy_name, public_names in {MODULE_EXPORTS!r}:
    scenario = importlib.import_module(
        "scenarios.bearing.cloud_diagnosis." + scenario_name
    )
    legacy = importlib.import_module("cloud_service." + legacy_name)
    assert tuple(legacy.__all__) == public_names
    for public_name in public_names:
        expected = getattr(scenario, public_name)
        assert getattr(compatibility, public_name) is expected
        assert getattr(legacy, public_name) is expected
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr


def test_legacy_cloud_moment_pickle_globals_resolve_without_scenario_preimport() -> None:
    code = """
import importlib
import pickle
import sys

assert "scenarios.bearing.cloud_diagnosis.moment_light_adapt" not in sys.modules
prediction_class = pickle.loads(
    b"ccloud_service.moment_light_adapt\\nMomentPrediction\\n."
)
policy_class = pickle.loads(
    b"ccloud_service.moment_light_adapt\\nMomentReviewPolicy\\n."
)
scenario = importlib.import_module(
    "scenarios.bearing.cloud_diagnosis.moment_light_adapt"
)
assert prediction_class is scenario.MomentPrediction
assert policy_class is scenario.MomentReviewPolicy
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr
