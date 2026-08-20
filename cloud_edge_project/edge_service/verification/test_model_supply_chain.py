from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from common.model_signing import sign_manifest
from edge_model.manifest_validation import validate_model_manifest
from edge_model.model_pull import ModelPullError, pull_candidate
from edge_runtime.config import EdgeRuntimeConfig
from edge_runtime.model_update_poller import ModelUpdatePoller


BASELINE_VERSION = "distilled_h5_kd_fold3_a9f20442"
BASELINE_DIR = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "distilled_h5"
    / BASELINE_VERSION
)


def _write_signing_keys(tmp_path: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _signed_bundle(
    private_key_path: Path,
    *,
    version: str,
    mutate_after_sign=None,  # noqa: ANN001
) -> bytes:
    manifest = json.loads((BASELINE_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest = sign_manifest(
        manifest,
        private_key_path=private_key_path,
        key_id="test-release-v1",
    )
    if mutate_after_sign is not None:
        mutate_after_sign(manifest)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel_path in manifest["files"]:
            archive.write(BASELINE_DIR / rel_path, arcname=rel_path)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )
    return payload.getvalue()


def test_pull_installs_only_a_valid_ed25519_signed_bundle(tmp_path: Path) -> None:
    private_key_path, public_key_path = _write_signing_keys(tmp_path)
    payload = _signed_bundle(private_key_path, version="signed-v2")

    result = pull_candidate(
        update_id="update-signed-v2",
        download_url="https://cloud.example/model.zip",
        target_version="signed-v2",
        model_root=tmp_path / "models",
        expected_sha256=json.loads(
            (BASELINE_DIR / "manifest.json").read_text(encoding="utf-8")
        )["files"]["best_model.pt"],
        signing_public_key_path=public_key_path,
        expected_signing_key_id="test-release-v1",
        http_get=lambda _: payload,
    )

    assert result["action"] == "installed"
    installed = tmp_path / "models" / "distilled_h5" / "signed-v2"
    manifest = validate_model_manifest(installed, expected_version="signed-v2")
    assert manifest["signature"]["algorithm"] == "Ed25519"


def test_pull_rejects_manifest_modified_after_signing(tmp_path: Path) -> None:
    private_key_path, public_key_path = _write_signing_keys(tmp_path)
    payload = _signed_bundle(
        private_key_path,
        version="signed-v2",
        mutate_after_sign=lambda manifest: manifest.__setitem__("model_family", "tampered"),
    )

    with pytest.raises(ModelPullError, match="MODEL_MANIFEST_SIGNATURE_MISMATCH"):
        pull_candidate(
            update_id="update-tampered",
            download_url="https://cloud.example/model.zip",
            target_version="signed-v2",
            model_root=tmp_path / "models",
            signing_public_key_path=public_key_path,
            expected_signing_key_id="test-release-v1",
            http_get=lambda _: payload,
        )


def test_default_model_download_requires_https(tmp_path: Path) -> None:
    _, public_key_path = _write_signing_keys(tmp_path)
    with pytest.raises(ModelPullError, match="MODEL_DOWNLOAD_HTTPS_REQUIRED"):
        pull_candidate(
            update_id="update-http",
            download_url="http://cloud.example/model.zip",
            target_version="v2",
            model_root=tmp_path / "models",
            signing_public_key_path=public_key_path,
            expected_signing_key_id="test-release-v1",
        )


def test_poller_rejects_plain_http_without_explicit_test_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MODEL_UPDATE_HTTPS_REQUIRED"):
        ModelUpdatePoller(
            cloud_base_url="http://cloud.example",
            edge_node_id="edge_01",
            model_root=tmp_path / "models",
            model_runtime=object(),
            signing_public_key_path=tmp_path / "public.pem",
            expected_signing_key_id="release-v1",
        )


def test_enabled_poller_configuration_requires_https_and_a_public_key(
    tmp_path: Path,
) -> None:
    insecure = EdgeRuntimeConfig.from_env(
        {
            "EDGE_MODEL_UPDATE_POLLER_ENABLED": "true",
            "CLOUD_SERVICE_BASE_URL": "http://cloud.example",
        }
    )
    assert "EDGE_MODEL_SIGNING_PUBLIC_KEY_FILE_REQUIRED" in insecure.validate()
    assert "EDGE_MODEL_UPDATE_HTTPS_REQUIRED" in insecure.validate()

    _, public_key_path = _write_signing_keys(tmp_path)
    secure = EdgeRuntimeConfig.from_env(
        {
            "EDGE_MODEL_UPDATE_POLLER_ENABLED": "true",
            "CLOUD_SERVICE_BASE_URL": "https://cloud.example",
            "EDGE_MODEL_SIGNING_PUBLIC_KEY_FILE": str(public_key_path),
        }
    )
    assert secure.validate() == []
