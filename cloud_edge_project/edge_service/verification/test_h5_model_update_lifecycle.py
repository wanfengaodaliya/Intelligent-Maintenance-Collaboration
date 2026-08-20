from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest

from edge_model.contracts import EdgeResult
from edge_model.h5_probe import H5ProbeError, default_probe_dir, load_h5_probe_task
from edge_model.local_h5_client import (
    H5ActivationError,
    LocalH5ClientConfig,
    LocalH5ModelClient,
)
from edge_model.manifest_validation import (
    ManifestValidationError,
    validate_model_manifest,
)
from edge_model.model_store import (
    MODEL_PIN_POLLER_CONFLICT,
    ModelStoreBootstrapError,
    initialize_model_store,
    validate_model_update_mode,
)
from edge_model.model_pull import ModelPullError, pull_candidate
from edge_model.version_store import read_active_version, set_active_version
from edge_runtime.config import EdgeRuntimeConfig
from edge_runtime.model_update_poller import ModelUpdatePoller


BASELINE_VERSION = "distilled_h5_kd_fold3_a9f20442"
EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_MODEL_ROOT = EDGE_SERVICE_ROOT / "models"
BASELINE_DIR = BUNDLED_MODEL_ROOT / "distilled_h5" / BASELINE_VERSION


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _edge_result(version: str) -> EdgeResult:
    return EdgeResult(
        edge_result="normal",
        confidence=0.8,
        edge_risk_level="low",
        model_version=version,
        diagnosis_label="healthy",
        class_probabilities={
            "healthy": 0.8,
            "outer_ring_damage": 0.1,
            "inner_ring_damage": 0.1,
        },
    )


class _FakeModel:
    feature_pipeline_version = "edge_feature_v1"

    def __init__(self, version: str, *, invalid_probe: bool = False) -> None:
        self.model_version = version
        self.invalid_probe = invalid_probe

    def run(self, task, cancel_event=None):  # noqa: ANN001
        del task, cancel_event
        result = _edge_result(self.model_version)
        if self.invalid_probe:
            result.class_probabilities["healthy"] = float("nan")
        return result

    def build_evidence(self, raw_packet):  # noqa: ANN001
        return {"raw_packet": raw_packet}


def test_probe_adapter_rebuilds_standard_raw_packet() -> None:
    task = load_h5_probe_task(default_probe_dir())

    assert task.raw_packet is not None
    data = task.raw_packet["data"]
    assert set(data) == {
        "vibration",
        "shaft_speed_rpm",
        "load_torque_nm",
        "bearing_radial_load_n",
        "bearing_module_temperature_c",
    }
    assert data["vibration"]["sample_rate_hz"] == 64_000
    assert data["vibration"]["sample_count"] == 3_200
    assert len(data["vibration"]["values"]) == 3_200
    assert data["shaft_speed_rpm"]["sample_count"] == 200
    assert task.request_id == "__h5_probe_request__"


def test_probe_adapter_rejects_npz_dtype_drift(tmp_path: Path) -> None:
    probe_dir = tmp_path / "probe"
    shutil.copytree(default_probe_dir(), probe_dir)
    npz_path = probe_dir / "probe.npz"
    with np.load(npz_path, allow_pickle=False) as source:
        arrays = {name: source[name] for name in source.files}
    arrays["vibration"] = arrays["vibration"].astype(np.float64)
    np.savez_compressed(npz_path, **arrays)
    manifest_path = probe_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact"]["sha256"] = _sha256(npz_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(H5ProbeError, match="PROBE_CHANNEL_DTYPE_INVALID=vibration"):
        load_h5_probe_task(probe_dir)


def test_manifest_validator_checks_every_listed_file(tmp_path: Path) -> None:
    model_dir = tmp_path / BASELINE_VERSION
    shutil.copytree(BASELINE_DIR, model_dir)
    manifest = validate_model_manifest(model_dir, expected_version=BASELINE_VERSION)
    assert "README.md" in manifest["files"]

    (model_dir / "README.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(
        ManifestValidationError, match="MODEL_MANIFEST_SHA256_MISMATCH=README.md"
    ):
        validate_model_manifest(model_dir, expected_version=BASELINE_VERSION)


def test_model_store_seeds_empty_root_and_rejects_dangling_pointer(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime-models"
    selection = initialize_model_store(
        model_root=runtime_root,
        bundled_model_root=BUNDLED_MODEL_ROOT,
        baseline_version=BASELINE_VERSION,
        pinned_version=None,
    )
    assert selection.version == BASELINE_VERSION
    assert read_active_version("distilled_h5", base=runtime_root) == BASELINE_VERSION
    validate_model_manifest(
        runtime_root / "distilled_h5" / BASELINE_VERSION,
        expected_version=BASELINE_VERSION,
    )

    set_active_version("distilled_h5", "missing-v2", base=runtime_root)
    with pytest.raises(ModelStoreBootstrapError, match="MODEL_ACTIVE_TARGET_INVALID"):
        initialize_model_store(
            model_root=runtime_root,
            bundled_model_root=BUNDLED_MODEL_ROOT,
            baseline_version=BASELINE_VERSION,
            pinned_version=None,
        )

    pinned = initialize_model_store(
        model_root=runtime_root,
        bundled_model_root=BUNDLED_MODEL_ROOT,
        baseline_version=BASELINE_VERSION,
        pinned_version=BASELINE_VERSION,
    )
    assert pinned.pinned is True
    assert pinned.version == BASELINE_VERSION


def test_pin_and_poller_conflict_is_fail_fast_configuration() -> None:
    with pytest.raises(ModelStoreBootstrapError, match=MODEL_PIN_POLLER_CONFLICT):
        validate_model_update_mode(pinned_version="v1", poller_enabled=True)

    default_config = EdgeRuntimeConfig.from_env({})
    assert default_config.model_update.enabled is False
    conflict = EdgeRuntimeConfig.from_env(
        {
            "EDGE_MODEL_VERSION": "v1",
            "EDGE_MODEL_UPDATE_POLLER_ENABLED": "true",
        }
    )
    assert MODEL_PIN_POLLER_CONFLICT in conflict.validate()

    with pytest.raises(ModelStoreBootstrapError, match="MODEL_VERSION_INVALID"):
        validate_model_update_mode(
            pinned_version="../escaped-version", poller_enabled=False
        )
    invalid_config = EdgeRuntimeConfig.from_env(
        {"EDGE_MODEL_VERSION": "../escaped-version"}
    )
    assert "MODEL_VERSION_INVALID" in invalid_config.validate()


def test_pull_rejects_path_components_before_disk_or_network(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    fetched = []
    with pytest.raises(ModelPullError, match="MODEL_UPDATE_ID_INVALID"):
        pull_candidate(
            update_id="../escaped-update",
            download_url="http://cloud/bundle",
            target_version="v2",
            model_root=model_root,
            http_get=lambda url: fetched.append(url) or b"",
        )
    assert fetched == []
    assert not model_root.exists()


def test_local_client_switches_handle_and_pointer_only_after_probe(
    tmp_path: Path,
) -> None:
    set_active_version("distilled_h5", "v1", base=tmp_path)

    def factory(*, model_dir: Path, model_version: str):  # noqa: ANN001
        del model_dir
        return _FakeModel(model_version)

    client = LocalH5ModelClient(
        LocalH5ClientConfig(model_root=tmp_path, initial_version="v1"),
        model_factory=factory,
    )
    assert client.readiness().model_version == "v1"

    activated = client.activate_version("v2")

    assert activated == {
        "runtime_version": "v2",
        "active_pointer_version": "v2",
    }
    assert client.current_version == "v2"
    assert read_active_version("distilled_h5", base=tmp_path) == "v2"


def test_invalid_candidate_probe_preserves_old_handle_and_pointer(
    tmp_path: Path,
) -> None:
    set_active_version("distilled_h5", "v1", base=tmp_path)

    def factory(*, model_dir: Path, model_version: str):  # noqa: ANN001
        del model_dir
        return _FakeModel(model_version, invalid_probe=model_version == "v2")

    client = LocalH5ModelClient(
        LocalH5ClientConfig(model_root=tmp_path, initial_version="v1"),
        model_factory=factory,
    )
    assert client.readiness().model_version == "v1"

    with pytest.raises(H5ActivationError, match="MODEL_PROBE_FAILED"):
        client.activate_version("v2")

    assert client.current_version == "v1"
    assert read_active_version("distilled_h5", base=tmp_path) == "v1"


def test_inflight_request_keeps_the_handle_version_it_started_with(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingV1(_FakeModel):
        def run(self, task, cancel_event=None):  # noqa: ANN001
            del task, cancel_event
            started.set()
            assert release.wait(5.0)
            return _edge_result(self.model_version)

    set_active_version("distilled_h5", "v1", base=tmp_path)

    def factory(*, model_dir: Path, model_version: str):  # noqa: ANN001
        del model_dir
        return _FakeModel(model_version)

    client = LocalH5ModelClient(
        LocalH5ClientConfig(model_root=tmp_path, initial_version="v1"),
        model_factory=factory,
    )
    client.attach_model_for_test(_BlockingV1("v1"))
    task = load_h5_probe_task(default_probe_dir())
    result_holder = []
    thread = threading.Thread(
        target=lambda: result_holder.append(client.infer_task(task)), daemon=True
    )
    thread.start()
    assert started.wait(2.0)

    client.activate_version("v2")
    release.set()
    thread.join(5.0)

    assert result_holder[0].edge.model_version == "v1"
    assert client.infer_task(task).edge.model_version == "v2"


def test_poller_reports_success_only_after_runtime_double_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = {
        "pending_pulls": [
            {
                "update_id": "update-1",
                "candidate_version": "v2",
                "model_type": "distilled_h5",
                "artifact_sha256": "a" * 64,
            }
        ],
        "pending_rollbacks": [],
    }
    posts = []

    class _Runtime:
        def activate_version(self, version: str) -> dict[str, str]:
            return {"runtime_version": version, "active_pointer_version": version}

    monkeypatch.setattr(
        "edge_runtime.model_update_poller.pull_candidate", lambda **kwargs: kwargs
    )
    poller = ModelUpdatePoller(
        cloud_base_url="http://cloud",
        edge_node_id="edge_01",
        model_root=tmp_path,
        model_runtime=_Runtime(),
        http_get=lambda _: json.dumps(pending).encode("utf-8"),
        http_post=lambda url, body: posts.append((url, body)) or {"ok": True},
    )

    summary = poller.poll_once()

    assert summary["activated"] == 1
    assert summary["reported"] == 1
    assert posts[-1][1] == {"status": "succeeded"}


def test_poller_acknowledges_rollback_after_runtime_double_confirmation(
    tmp_path: Path,
) -> None:
    pending = {
        "pending_pulls": [],
        "pending_rollbacks": [
            {
                "update_id": "update-rollback-1",
                "rollback_target_version": "v1",
            }
        ],
    }
    activations = []
    posts = []

    class _Runtime:
        def activate_version(self, version: str) -> dict[str, str]:
            activations.append(version)
            return {
                "runtime_version": version,
                "active_pointer_version": version,
            }

    poller = ModelUpdatePoller(
        cloud_base_url="http://cloud",
        edge_node_id="edge_01",
        model_root=tmp_path,
        model_runtime=_Runtime(),
        http_get=lambda _: json.dumps(pending).encode("utf-8"),
        http_post=lambda url, body: posts.append((url, body)) or {"ok": True},
    )

    summary = poller.poll_once()

    assert activations == ["v1"]
    assert summary["rolled_back"] == 1
    assert summary["rollback_ack_failed"] == 0
    assert posts == [
        (
            "http://cloud/cloud/model-update/update-rollback-1/rollback-result",
            {
                "status": "succeeded",
                "edge_node_id": "edge_01",
                "rollback_target_version": "v1",
            },
        )
    ]


def test_poller_health_retains_stable_pending_query_error(tmp_path: Path) -> None:
    class _Runtime:
        def activate_version(self, version: str):  # noqa: ANN001
            raise AssertionError(version)

    poller = ModelUpdatePoller(
        cloud_base_url="http://cloud",
        edge_node_id="edge_01",
        model_root=tmp_path,
        model_runtime=_Runtime(),
        http_get=lambda _: (_ for _ in ()).throw(OSError("offline")),
    )

    assert poller.run_once() is True
    health = poller.health()
    assert health["last_error_code"] == "PENDING_QUERY_FAILED"
    assert health["last_summary"]["last_error_code"] == "PENDING_QUERY_FAILED"
