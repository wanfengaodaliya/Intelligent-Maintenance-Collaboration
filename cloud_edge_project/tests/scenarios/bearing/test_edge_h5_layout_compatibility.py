from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
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
        atol=1e-6,
    )
    assert tuple(fused.shape) == (1, 176)
    torch.testing.assert_close(
        fused.detach()[0, torch.tensor([0, 175])],
        torch.tensor([0.0948042497, 0.0090928469]),
        rtol=0,
        atol=1e-6,
    )
    assert float(fused.detach().sum()) == pytest.approx(14.7061004639, abs=1e-6)


def _scenario_model():
    module = importlib.import_module(
        "scenarios.bearing.edge_inference.h5.distilled_h5_model"
    )
    return module.DistilledH5DiagnosticModel(
        MODEL_DIR,
        model_version=MODEL_VERSION,
    )


def _probe_packet_with_currents() -> tuple[object, dict[str, object]]:
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
    return task, packet


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
    _, packet = _probe_packet_with_currents()
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
        abs=1e-6,
    )
    assert evidence["features"]["current_relationship"] == pytest.approx(
        {"current_imbalance_ratio": 0.2222225256},
        abs=1e-6,
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
            abs=1e-6,
        )
    )


def _set_packet_value(
    packet: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    target = packet
    for name in path[:-1]:
        target = target[name]  # type: ignore[assignment,index]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "operation", "message"),
    (
        (("data",), None, "prepare", "raw packet data must be an object"),
        (
            ("data", "vibration", "sample_rate_hz"),
            16_000,
            "prepare",
            "vibration must be 64000 Hz / 3200 samples",
        ),
        (
            ("data", "vibration", "sample_count"),
            3_199,
            "prepare",
            "vibration must be 64000 Hz / 3200 samples",
        ),
        (
            ("data", "vibration", "values"),
            [0.0] * 3_199,
            "prepare",
            "vibration values must be finite 3200-sample data",
        ),
        (
            ("data", "vibration", "values"),
            [0.0] * 3_199 + [float("nan")],
            "prepare",
            "vibration values must be finite 3200-sample data",
        ),
        (
            ("data", "vibration", "values"),
            ["invalid"] * 3_200,
            "prepare",
            "vibration values must be numeric",
        ),
        (
            ("data", "shaft_speed_rpm", "sample_count"),
            199,
            "prepare",
            "shaft_speed_rpm must be 4000 Hz / 200 samples",
        ),
        (
            ("data", "bearing_module_temperature_c"),
            float("nan"),
            "prepare",
            "bearing_module_temperature_c must be finite",
        ),
        (("device_id",), "", "evidence", "raw packet identity is invalid"),
        (("sequence_number",), 0, "evidence", "raw packet sequence_number is invalid"),
        (("end_generate_timestamp_ns",), 0, "evidence", "end_generate_timestamp_ns must be positive"),
        (
            ("data", "phase_current_1_A"),
            None,
            "evidence",
            "raw packet phase_current_1_A must be an object",
        ),
        (
            ("data", "phase_current_1_A", "values"),
            [0.0] * 3_199 + [float("inf")],
            "evidence",
            "phase_current_1_A values must be finite 3200-sample data",
        ),
    ),
    ids=(
        "data-object",
        "vibration-rate",
        "vibration-count",
        "vibration-shape",
        "vibration-non-finite",
        "vibration-non-numeric",
        "condition-count",
        "temperature-non-finite",
        "identity",
        "sequence",
        "timestamp",
        "current-object",
        "current-non-finite",
    ),
)
def test_distilled_h5_input_errors_match_frozen_contract(
    path: tuple[str, ...],
    value: object,
    operation: str,
    message: str,
) -> None:
    _, packet = _probe_packet_with_currents()
    _set_packet_value(packet, path, value)
    model = _scenario_model()
    target = model.prepare_inputs if operation == "prepare" else model.build_evidence

    with pytest.raises(ValueError) as error:
        target(packet)

    assert str(error.value) == message


def test_distilled_h5_run_errors_and_probability_guard_match_frozen_contract() -> None:
    task, _ = _probe_packet_with_currents()
    model = _scenario_model()

    with pytest.raises(ValueError) as raw_packet_error:
        model.run(replace(task, raw_packet=None))
    assert str(raw_packet_error.value) == (
        "distilled H5 requires the validated raw packet"
    )

    class _InvalidProbabilityNetwork:
        def __call__(self, vibration, physical, condition):  # noqa: ANN001
            del vibration, physical, condition
            return torch.full((1, 3), float("nan")), torch.empty((1, 0))

    model.model = _InvalidProbabilityNetwork()
    with pytest.raises(ValueError) as probability_error:
        model.run(task)
    assert str(probability_error.value) == "distilled H5 probabilities are invalid"


def test_distilled_h5_all_cancellation_checkpoints_match_frozen_contract() -> None:
    from edge_model.contracts import InferenceCancelled

    task, _ = _probe_packet_with_currents()
    model = _scenario_model()

    class _CancelOnCheck:
        def __init__(self, trigger: int) -> None:
            self.trigger = trigger
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls == self.trigger

    for trigger in range(1, 5):
        cancel_event = _CancelOnCheck(trigger)
        with pytest.raises(InferenceCancelled) as error:
            model.run(task, cancel_event=cancel_event)
        assert str(error.value) == "distilled H5 inference cancelled"
        assert cancel_event.calls == trigger


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _update_manifest_hash(model_dir: Path, filename: str) -> None:
    manifest_path = model_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][filename] = _sha256(model_dir / filename)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_distilled_h5_manifest_errors_match_frozen_contract(tmp_path: Path) -> None:
    from scenarios.bearing.edge_inference.h5.distilled_h5_model import (
        H5ModelArtifactError,
        DistilledH5DiagnosticModel,
    )

    with pytest.raises(H5ModelArtifactError) as missing_error:
        DistilledH5DiagnosticModel(
            tmp_path / "missing",
            model_version=MODEL_VERSION,
        )
    assert str(missing_error.value) == "MODEL_MANIFEST_DIR_MISSING"

    with pytest.raises(H5ModelArtifactError) as version_error:
        DistilledH5DiagnosticModel(MODEL_DIR, model_version="unexpected-version")
    assert str(version_error.value) == (
        "MODEL_MANIFEST_VERSION_MISMATCH: expected=unexpected-version "
        f"got={MODEL_VERSION}"
    )

    inconsistent_dir = tmp_path / MODEL_VERSION
    shutil.copytree(MODEL_DIR, inconsistent_dir)
    (inconsistent_dir / "README.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(H5ModelArtifactError) as hash_error:
        DistilledH5DiagnosticModel(
            inconsistent_dir,
            model_version=MODEL_VERSION,
        )
    assert str(hash_error.value) == "MODEL_MANIFEST_SHA256_MISMATCH=README.md"


def test_distilled_h5_normalization_error_matches_frozen_contract(
    tmp_path: Path,
) -> None:
    from scenarios.bearing.edge_inference.h5.distilled_h5_model import (
        H5ModelArtifactError,
        DistilledH5DiagnosticModel,
    )

    model_dir = tmp_path / MODEL_VERSION
    shutil.copytree(MODEL_DIR, model_dir)
    normalization_path = model_dir / "condition_norm.json"
    normalization_path.write_text(
        json.dumps({"mean": [0.0] * 13, "std": [0.0] * 13}),
        encoding="utf-8",
    )
    _update_manifest_hash(model_dir, "condition_norm.json")

    with pytest.raises(H5ModelArtifactError) as error:
        DistilledH5DiagnosticModel(model_dir, model_version=MODEL_VERSION)

    assert str(error.value) == (
        "MODEL_MANIFEST_NORMALIZATION_STD_INVALID: condition_norm.json"
    )


def test_distilled_h5_missing_checkpoint_weights_match_frozen_contract(
    tmp_path: Path,
) -> None:
    from scenarios.bearing.edge_inference.h5.distilled_h5_model import (
        H5ModelArtifactError,
        DistilledH5DiagnosticModel,
    )

    model_dir = tmp_path / MODEL_VERSION
    shutil.copytree(MODEL_DIR, model_dir)
    checkpoint_path = model_dir / "best_model.pt"
    torch.save({"model_state_dict": {"other.weight": torch.ones(1)}}, checkpoint_path)
    checksum_path = model_dir / "checkpoint_sha256.txt"
    checksum_path.write_text(_sha256(checkpoint_path) + "\n", encoding="utf-8")
    _update_manifest_hash(model_dir, "best_model.pt")
    _update_manifest_hash(model_dir, "checkpoint_sha256.txt")

    with pytest.raises(H5ModelArtifactError) as error:
        DistilledH5DiagnosticModel(model_dir, model_version=MODEL_VERSION)

    assert str(error.value) == "distilled H5 checkpoint cannot be loaded"


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(PROJECT_ROOT), str(EDGE_SERVICE_SRC))
    )
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
        "edge_diagnosis.h5_features",
        "edge_diagnosis.h5_network",
        "edge_diagnosis.distilled_h5_model",
        "scenarios.bearing.edge_inference.h5.features",
        "scenarios.bearing.edge_inference.h5.network",
        "scenarios.bearing.edge_inference.h5.distilled_h5_model",
        "compatibility.bearing_v12.edge_h5_exports",
    ),
)
def test_h5_modules_support_cold_import_orders(entry_import: str) -> None:
    code = f"""
import importlib
import sys

assert "scenarios.bearing.edge_inference.h5.distilled_h5_model" not in sys.modules
importlib.import_module({entry_import!r})
compatibility = importlib.import_module("compatibility.bearing_v12.edge_h5_exports")
for scenario_name, legacy_name, public_names in {MODULE_EXPORTS!r}:
    scenario = importlib.import_module(
        "scenarios.bearing.edge_inference.h5." + scenario_name
    )
    legacy = importlib.import_module("edge_diagnosis." + legacy_name)
    assert tuple(legacy.__all__) == public_names
    for public_name in public_names:
        expected = getattr(scenario, public_name)
        assert getattr(compatibility, public_name) is expected
        assert getattr(legacy, public_name) is expected
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr


def test_legacy_h5_pickle_globals_resolve_without_scenario_preimport() -> None:
    code = """
import importlib
import pickle
import sys

assert "scenarios.bearing.edge_inference.h5.network" not in sys.modules
assert "scenarios.bearing.edge_inference.h5.distilled_h5_model" not in sys.modules
network_class = pickle.loads(
    b"cedge_diagnosis.h5_network\\nPhysicalFusionModel\\n."
)
model_class = pickle.loads(
    b"cedge_diagnosis.distilled_h5_model\\nDistilledH5DiagnosticModel\\n."
)
scenario_network = importlib.import_module(
    "scenarios.bearing.edge_inference.h5.network"
)
scenario_model = importlib.import_module(
    "scenarios.bearing.edge_inference.h5.distilled_h5_model"
)
assert network_class is scenario_network.PhysicalFusionModel
assert model_class is scenario_model.DistilledH5DiagnosticModel
"""

    completed = _run_isolated(code)

    assert completed.returncode == 0, completed.stderr


def test_local_h5_client_loads_scenario_model_through_legacy_path() -> None:
    from edge_model.local_h5_client import LocalH5ModelClient
    from scenarios.bearing.edge_inference.h5.distilled_h5_model import (
        DistilledH5DiagnosticModel,
    )

    model = LocalH5ModelClient._load_distilled_h5(
        model_dir=MODEL_DIR,
        model_version=MODEL_VERSION,
    )

    assert type(model) is DistilledH5DiagnosticModel
