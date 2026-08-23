from __future__ import annotations

import copy
import importlib
import os
import pickle
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest


torch = pytest.importorskip("torch", reason="edge H5 layout tests require torch")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EDGE_SERVICE_SRC = PROJECT_ROOT / "edge_service" / "src"
if str(EDGE_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(EDGE_SERVICE_SRC))
MODEL_VERSION = "distilled_h5_kd_fold3_a9f20442"
MODEL_DIR = (
    PROJECT_ROOT
    / "edge_service"
    / "models"
    / "distilled_h5"
    / MODEL_VERSION
)
MODULE_EXPORTS = (
    ("features", "h5_features", ("_compute_single", "normalize_features")),
    ("network", "h5_network", ("PhysicalFusionModel",)),
    (
        "distilled_h5_model",
        "distilled_h5_model",
        (
            "H5_LABELS",
            "RUNTIME_MODEL_VERSION",
            "H5ModelArtifactError",
            "DistilledH5DiagnosticModel",
        ),
    ),
)


@pytest.mark.parametrize(
    ("scenario_module_name", "legacy_module_name", "public_names"),
    MODULE_EXPORTS,
)
def test_legacy_h5_exports_are_scenario_objects(
    scenario_module_name: str,
    legacy_module_name: str,
    public_names: tuple[str, ...],
) -> None:
    scenario_module = importlib.import_module(
        f"scenarios.bearing.edge_inference.h5.{scenario_module_name}"
    )
    compatibility_module = importlib.import_module(
        "compatibility.bearing_v12.edge_h5_exports"
    )
    legacy_module = importlib.import_module(
        f"edge_diagnosis.{legacy_module_name}"
    )

    assert tuple(legacy_module.__all__) == public_names
    assert set(public_names).issubset(compatibility_module.__all__)
    for public_name in public_names:
        scenario_value = getattr(scenario_module, public_name)
        assert getattr(compatibility_module, public_name) is scenario_value
        assert getattr(legacy_module, public_name) is scenario_value


def test_h5_feature_vector_and_normalization_match_frozen_goldens() -> None:
    from scenarios.bearing.edge_inference.h5.features import (
        _compute_single,
        normalize_features,
    )

    indexes = np.arange(3_200)
    vibration = (
        0.35 * np.sin(2 * np.pi * 1_000 * indexes / 64_000)
        + 0.1 * np.cos(2 * np.pi * 4_200 * indexes / 64_000)
    ).astype(np.float32)
    expected = np.asarray(
        [
            0.2573907673358917,
            0.2573907673358917,
            0.895666241645813,
            -1.2906728982925415,
            0.0,
            1.7398959398269653,
            1.9691215753555298,
            1.131746768951416,
            1241.5093994140625,
            845.3619384765625,
            1.1354202032089233,
            1000.0,
            0.9245283007621765,
            0.07547169923782349,
            3.060578854358043e-16,
            0.36400550603866577,
            -1.463871955871582,
            0.45000001788139343,
            3200.0,
        ],
        dtype=np.float32,
    )

    features = _compute_single(vibration)

    np.testing.assert_array_equal(features, expected)
    assert features.shape == (19,)
    assert features.dtype == np.float32
    mean = np.arange(19, dtype=np.float32) / 10
    std = np.arange(1, 20, dtype=np.float32) / 5
    expected_normalized = ((expected - mean) / std).astype(np.float32)
    np.testing.assert_array_equal(
        normalize_features(features, mean, std),
        expected_normalized,
    )


def test_h5_network_structure_and_forward_match_frozen_goldens() -> None:
    from scenarios.bearing.edge_inference.h5.network import PhysicalFusionModel

    expected_state_shapes = {
        "cnn.0.weight": (32, 1, 3),
        "cnn.0.bias": (32,),
        "cnn.1.weight": (32,),
        "cnn.1.bias": (32,),
        "cnn.1.running_mean": (32,),
        "cnn.1.running_var": (32,),
        "cnn.1.num_batches_tracked": (),
        "cnn.4.weight": (64, 32, 3),
        "cnn.4.bias": (64,),
        "cnn.5.weight": (64,),
        "cnn.5.bias": (64,),
        "cnn.5.running_mean": (64,),
        "cnn.5.running_var": (64,),
        "cnn.5.num_batches_tracked": (),
        "cnn.8.weight": (128, 64, 3),
        "cnn.8.bias": (128,),
        "cnn.9.weight": (128,),
        "cnn.9.bias": (128,),
        "cnn.9.running_mean": (128,),
        "cnn.9.running_var": (128,),
        "cnn.9.num_batches_tracked": (),
        "phys_encoder.0.weight": (32, 19),
        "phys_encoder.0.bias": (32,),
        "phys_encoder.1.weight": (32,),
        "phys_encoder.1.bias": (32,),
        "condition_encoder.0.weight": (16, 13),
        "condition_encoder.0.bias": (16,),
        "condition_encoder.1.weight": (16,),
        "condition_encoder.1.bias": (16,),
        "classifier.0.weight": (64, 176),
        "classifier.0.bias": (64,),
        "classifier.3.weight": (3, 64),
        "classifier.3.bias": (3,),
    }
    torch.manual_seed(20_260_823)
    model = PhysicalFusionModel()
    model.eval()

    assert model.vibration_dim == 128
    assert model.phys_dim == 32
    assert model.condition_dim == 16
    assert model.cond_scale == 0.25
    assert model.cond_dropout == 0.5
    assert model.use_h4_cnn is False
    assert {
        name: tuple(value.shape) for name, value in model.state_dict().items()
    } == expected_state_shapes

    logits, fused = model(
        torch.linspace(-1, 1, 800).reshape(1, 800),
        torch.linspace(-0.5, 0.5, 19).reshape(1, 19),
        torch.linspace(-0.25, 0.25, 13).reshape(1, 13),
    )

    torch.testing.assert_close(
        logits,
        torch.tensor([[0.0577577762, -0.0212971549, -0.0545240901]]),
        rtol=0,
        atol=1e-8,
    )
    assert tuple(fused.shape) == (1, 176)
    torch.testing.assert_close(
        fused.detach()[0, torch.tensor([0, 175])],
        torch.tensor([0.0948042497, 0.0090928469]),
        rtol=0,
        atol=1e-8,
    )
    assert float(fused.detach().sum()) == pytest.approx(14.7061004639, abs=1e-8)


def _scenario_model():
    module = importlib.import_module(
        "scenarios.bearing.edge_inference.h5.distilled_h5_model"
    )
    return module.DistilledH5DiagnosticModel(
        MODEL_DIR,
        model_version=MODEL_VERSION,
    )


def test_distilled_h5_probe_tensors_and_result_match_frozen_goldens() -> None:
    from edge_model.h5_probe import default_probe_dir, load_h5_probe_task

    task = load_h5_probe_task(default_probe_dir())
    model = _scenario_model()
    vibration, physical, condition = model.prepare_inputs(task.raw_packet)

    assert [tuple(value.shape) for value in (vibration, physical, condition)] == [
        (1, 800),
        (1, 19),
        (1, 13),
    ]
    assert [value.dtype for value in (vibration, physical, condition)] == [
        torch.float32,
        torch.float32,
        torch.float32,
    ]
    np.testing.assert_allclose(
        [float(value.sum()) for value in (vibration, physical, condition)],
        [-17.8794593811, 9.7410831451, 0.0078298450],
        rtol=0,
        atol=1e-6,
    )
    np.testing.assert_allclose(
        [
            float(vibration[0, 0]),
            float(vibration[0, -1]),
            float(physical[0, 0]),
            float(physical[0, -1]),
            float(condition[0, 0]),
            float(condition[0, -1]),
        ],
        [
            0.0185479484,
            0.0605179407,
            0.3624104261,
            -0.1547767967,
            -1.7302238941,
            0.0583707429,
        ],
        rtol=0,
        atol=1e-6,
    )

    result = model.run(task)

    assert result.edge_result == "normal"
    assert result.confidence == 0.996627
    assert result.edge_risk_level == "low"
    assert result.model_version == MODEL_VERSION
    assert result.diagnosis_label == "healthy"
    assert result.class_probabilities == {
        "healthy": 0.996627,
        "outer_ring_damage": 0.002102,
        "inner_ring_damage": 0.001271,
    }


def test_distilled_h5_evidence_and_errors_match_frozen_goldens() -> None:
    from edge_model.contracts import InferenceCancelled
    from edge_model.h5_probe import default_probe_dir, load_h5_probe_task

    task = load_h5_probe_task(default_probe_dir())
    assert task.raw_packet is not None
    packet = copy.deepcopy(task.raw_packet)
    packet["data"]["phase_current_1_A"] = {
        "sample_rate_hz": 64_000,
        "sample_count": 3_200,
        "values": [1.0] * 3_200,
        "unit": "A",
    }
    packet["data"]["phase_current_2_A"] = {
        "sample_rate_hz": 64_000,
        "sample_count": 3_200,
        "values": [1.25] * 3_200,
        "unit": "A",
    }
    model = _scenario_model()

    evidence = model.build_evidence(packet)

    assert evidence["perception_quality"] == {"status": "good", "flags": []}
    assert evidence["features"]["vibration"] == pytest.approx(
        {
            "source_sample_rate_hz": 64_000,
            "analysis_sample_rate_hz": 16_000,
            "rms": 0.1723001212,
            "absolute_peak": 0.9667879939,
            "kurtosis": 3.0252528191,
            "dominant_frequency_hz": 1860.0,
            "band_power_ratio_500_2000": 0.2221473637,
            "spectral_entropy": 0.9360470475,
            "unit": "mm/s",
        },
        abs=1e-9,
    )
    assert evidence["features"]["current_relationship"] == pytest.approx(
        {"current_imbalance_ratio": 0.2222225256},
        abs=1e-9,
    )
    assert evidence["features"]["operating_context"]["shaft_speed_rpm"] == (
        pytest.approx(
            {
                "mean": 899.7917724609,
                "last": 899.7760620117,
                "minimum": 899.7760620117,
                "maximum": 899.8059082031,
                "standard_deviation": 0.0086553404,
            },
            abs=1e-9,
        )
    )

    with pytest.raises(ValueError, match="distilled H5 requires the validated raw packet"):
        model.run(replace(task, raw_packet=None))
    invalid_packet = copy.deepcopy(packet)
    invalid_packet["data"]["vibration"]["sample_rate_hz"] = 16_000
    with pytest.raises(ValueError, match="vibration must be 64000 Hz / 3200 samples"):
        model.prepare_inputs(invalid_packet)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(
        InferenceCancelled,
        match="distilled H5 inference cancelled",
    ):
        model.run(task, cancel_event=cancelled)


@pytest.mark.parametrize(
    "imports",
    (
        (
            "edge_diagnosis.h5_features",
            "edge_diagnosis.h5_network",
            "edge_diagnosis.distilled_h5_model",
        ),
        (
            "scenarios.bearing.edge_inference.h5.features",
            "scenarios.bearing.edge_inference.h5.network",
            "scenarios.bearing.edge_inference.h5.distilled_h5_model",
        ),
        ("compatibility.bearing_v12.edge_h5_exports",),
    ),
)
def test_h5_modules_support_cold_import_orders(imports: tuple[str, ...]) -> None:
    code = "; ".join(f"import {module_name}" for module_name in imports)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(PROJECT_ROOT), str(EDGE_SERVICE_SRC))
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_legacy_h5_pickle_globals_resolve_to_scenario_classes() -> None:
    scenario_network = importlib.import_module(
        "scenarios.bearing.edge_inference.h5.network"
    )
    scenario_model = importlib.import_module(
        "scenarios.bearing.edge_inference.h5.distilled_h5_model"
    )

    assert (
        pickle.loads(b"cedge_diagnosis.h5_network\nPhysicalFusionModel\n.")
        is scenario_network.PhysicalFusionModel
    )
    assert (
        pickle.loads(
            b"cedge_diagnosis.distilled_h5_model\n"
            b"DistilledH5DiagnosticModel\n."
        )
        is scenario_model.DistilledH5DiagnosticModel
    )
