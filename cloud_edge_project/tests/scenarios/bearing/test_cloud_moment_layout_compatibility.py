from __future__ import annotations

import importlib
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
    with pytest.raises(KeyError, match="^'unsupported'$" ):
        policy.decide("unsupported")
    with pytest.raises(KeyError, match="^'shaft_speed_rpm'$" ):
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
