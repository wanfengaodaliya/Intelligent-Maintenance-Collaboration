# -*- coding: utf-8 -*-
"""Ed25519 signing helpers for model bundle manifests."""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


MODEL_BUNDLE_SCHEMA = "edge-model-bundle/1"
SIGNATURE_ALGORITHM = "Ed25519"


class ModelSigningError(RuntimeError):
    """A model manifest could not be signed or authenticated."""


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Return the exact bytes covered by the detached manifest signature."""
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelSigningError("MODEL_MANIFEST_CANONICALIZATION_FAILED") from exc


def sign_manifest(
    manifest: Mapping[str, Any],
    *,
    private_key_path: Path | str,
    key_id: str,
) -> dict[str, Any]:
    """Return a copy of *manifest* signed by a PEM Ed25519 private key."""
    if not key_id or len(key_id) > 128:
        raise ModelSigningError("MODEL_SIGNING_KEY_ID_INVALID")
    signed = dict(manifest)
    signed["schema_version"] = MODEL_BUNDLE_SCHEMA
    signed.pop("signature", None)
    try:
        key_data = Path(private_key_path).read_bytes()
        private_key = serialization.load_pem_private_key(key_data, password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise ModelSigningError("MODEL_SIGNING_PRIVATE_KEY_INVALID") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ModelSigningError("MODEL_SIGNING_PRIVATE_KEY_NOT_ED25519")
    signature = private_key.sign(canonical_manifest_bytes(signed))
    signed["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def verify_manifest_signature(
    manifest: Mapping[str, Any],
    *,
    public_key_path: Path | str,
    expected_key_id: str,
) -> None:
    """Authenticate a signed manifest with a mounted PEM Ed25519 public key."""
    if manifest.get("schema_version") != MODEL_BUNDLE_SCHEMA:
        raise ModelSigningError("MODEL_BUNDLE_SCHEMA_UNSUPPORTED")
    signature = manifest.get("signature")
    if not isinstance(signature, dict):
        raise ModelSigningError("MODEL_MANIFEST_SIGNATURE_MISSING")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise ModelSigningError("MODEL_MANIFEST_SIGNATURE_ALGORITHM_INVALID")
    if signature.get("key_id") != expected_key_id:
        raise ModelSigningError("MODEL_MANIFEST_SIGNING_KEY_MISMATCH")
    encoded = signature.get("value")
    if not isinstance(encoded, str):
        raise ModelSigningError("MODEL_MANIFEST_SIGNATURE_INVALID")
    try:
        signature_bytes = base64.b64decode(encoded, validate=True)
        key_data = Path(public_key_path).read_bytes()
        public_key = serialization.load_pem_public_key(key_data)
    except (OSError, TypeError, ValueError, binascii.Error) as exc:
        raise ModelSigningError("MODEL_SIGNING_PUBLIC_KEY_INVALID") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ModelSigningError("MODEL_SIGNING_PUBLIC_KEY_NOT_ED25519")
    try:
        public_key.verify(signature_bytes, canonical_manifest_bytes(manifest))
    except InvalidSignature as exc:
        raise ModelSigningError("MODEL_MANIFEST_SIGNATURE_MISMATCH") from exc
