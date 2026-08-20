from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import cloud_service.app as app_module
from common.model_signing import verify_manifest_signature
from cloud_service.config import load_cloud_settings
from cloud_service.storage.database import connect, initialize_database


def _insert_task(
    database_path: Path,
    *,
    update_id: str,
    status: str,
    artifact_path: str,
    artifact_sha256: str,
    artifact_bundle: list[dict] | None,
    candidate_version: str = "edge_v2",
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO model_update_task(
                   update_id, analysis_id, problem_id, scenario_type, subject_id,
                   problem_type, model_type, problem_context_json, evidence_snapshot_json,
                   baseline_version, candidate_version, candidate_artifact_json, status,
                   created_at_ns, updated_at_ns
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                update_id, "analysis_dl_001", "problem_dl_001", "bearing",
                "machine_01", "risk_underestimation", "distilled_h5",
                json.dumps({"operating_condition": "high_load"}),
                json.dumps({"sample_count": 20}),
                "edge_v1", candidate_version,
                json.dumps(
                    {
                        "candidate_version": candidate_version,
                        "artifact_path": artifact_path,
                        "artifact_sha256": artifact_sha256,
                        "artifact_bundle": artifact_bundle,
                        "model_type": "distilled_h5",
                        "feature_pipeline_version": "edge_feature_v1",
                        "input_feature_schema": {
                            "vibration.kurtosis": "number",
                            "vibration.rms": "number",
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                status, 1, time.time_ns(),
            ),
        )


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "cloud.db"
    initialize_database(database_path)
    settings = replace(
        load_cloud_settings(), backend="moment_light_adapt", database_path=database_path
    )
    monkeypatch.setattr(app_module, "load_cloud_settings", lambda: settings)
    private_key = Ed25519PrivateKey.generate()
    private_key_path = tmp_path / "model-signing-private.pem"
    public_key_path = tmp_path / "model-signing-public.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("CLOUD_MODEL_SIGNING_PRIVATE_KEY_FILE", str(private_key_path))
    monkeypatch.setenv("MODEL_UPDATE_SIGNING_KEY_ID", "test-release-v1")
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as test_client:
        yield test_client


def test_download_bundle_artifact_returns_zip_with_manifest(
    client, tmp_path, monkeypatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "best_model.pt").write_bytes(b"checkpoint")
    (bundle_dir / "condition_norm.json").write_text("{}", encoding="utf-8")
    entries = []
    for name in ("best_model.pt", "condition_norm.json"):
        digest = hashlib.sha256((bundle_dir / name).read_bytes()).hexdigest()
        entries.append({"rel_path": name, "sha256": digest})
    _insert_task(
        tmp_path / "cloud.db",
        update_id="update_dl_bundle",
        status="handoff_to_distribution",
        artifact_path=str(bundle_dir),
        artifact_sha256=hashlib.sha256(b"checkpoint").hexdigest(),
        artifact_bundle={"entries": entries, "artifact_sha256": hashlib.sha256(b"checkpoint").hexdigest()},
    )

    response = client.get("/cloud/model-update/update_dl_bundle/file")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"best_model.pt", "condition_norm.json", "manifest.json"} <= names
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["version"] == "edge_v2"
    assert manifest["model_type"] == "distilled_h5"
    assert manifest["files"]["best_model.pt"] == hashlib.sha256(b"checkpoint").hexdigest()
    verify_manifest_signature(
        manifest,
        public_key_path=tmp_path / "model-signing-public.pem",
        expected_key_id="test-release-v1",
    )

    monkeypatch.delenv("CLOUD_MODEL_SIGNING_PRIVATE_KEY_FILE")
    unsigned_response = client.get("/cloud/model-update/update_dl_bundle/file")
    assert unsigned_response.status_code == 503
    assert unsigned_response.json() == {"error_code": "MODEL_SIGNING_UNAVAILABLE"}


def test_download_single_file_artifact_streams_bytes(client, tmp_path) -> None:
    artifact = tmp_path / "candidate.pt"
    artifact.write_bytes(b"single-checkpoint")
    _insert_task(
        tmp_path / "cloud.db",
        update_id="update_dl_single",
        status="approved",
        artifact_path=str(artifact),
        artifact_sha256=hashlib.sha256(b"single-checkpoint").hexdigest(),
        artifact_bundle=None,
    )

    response = client.get("/cloud/model-update/update_dl_single/file")

    assert response.status_code == 200
    assert response.content == b"single-checkpoint"


def test_download_unknown_update_returns_404(client) -> None:
    response = client.get("/cloud/model-update/update_missing/file")
    assert response.status_code == 404
    assert response.json()["error_code"] == "UPDATE_NOT_FOUND"


def test_download_artifact_not_ready_returns_400(client, tmp_path) -> None:
    artifact = tmp_path / "candidate.pt"
    artifact.write_bytes(b"data")
    _insert_task(
        tmp_path / "cloud.db",
        update_id="update_dl_notready",
        status="created",
        artifact_path=str(artifact),
        artifact_sha256=hashlib.sha256(b"data").hexdigest(),
        artifact_bundle=None,
    )

    response = client.get("/cloud/model-update/update_dl_notready/file")

    assert response.status_code == 400
    assert response.json()["error_code"] == "ARTIFACT_NOT_READY"
