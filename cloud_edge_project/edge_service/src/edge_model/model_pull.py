# -*- coding: utf-8 -*-
"""从 Cloud 下载并原子安装候选模型；安装阶段不切换活动版本。"""
from __future__ import annotations

import io
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .manifest_validation import ManifestValidationError, validate_model_manifest
from .version_store import VersionStoreError, validate_path_component, version_dir


_ZIP_MAGIC = b"PK\x03\x04"


class ModelPullError(RuntimeError):
    """候选模型无法下载、校验或安装。"""


def _default_http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def pull_candidate(
    *,
    update_id: str,
    download_url: str,
    target_version: str,
    model_type: str = "distilled_h5",
    model_root: Path | str,
    expected_sha256: str | None = None,
    http_get: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """下载并安装候选版本目录，不修改 ``active_version.json``。"""
    fetch = http_get or _default_http_get
    root = Path(model_root)
    try:
        update_id = validate_path_component(update_id, field="MODEL_UPDATE_ID")
        target_version = validate_path_component(
            target_version, field="MODEL_VERSION"
        )
        model_type = validate_path_component(model_type, field="MODEL_TYPE")
    except VersionStoreError as exc:
        raise ModelPullError(str(exc)) from exc
    staging_root = version_dir(model_type, ".staging", base=root)
    staging = staging_root / update_id
    target = version_dir(model_type, target_version, base=root)
    _remove_staging(staging, staging_root)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        payload = fetch(download_url)
        if not payload.startswith(_ZIP_MAGIC):
            raise ModelPullError("MODEL_BUNDLE_ZIP_REQUIRED")
        _unpack_bundle(payload, staging)
        manifest = validate_model_manifest(
            staging,
            expected_model_type=model_type,
            expected_version=target_version,
        )
        if expected_sha256:
            primary = manifest["files"].get("best_model.pt")
            if not isinstance(primary, str) or primary.lower() != expected_sha256.lower():
                raise ModelPullError("BUNDLE_PRIMARY_SHA256_MISMATCH")
    except ManifestValidationError as exc:
        _remove_staging(staging, staging_root)
        raise ModelPullError(str(exc)) from exc
    except ModelPullError:
        _remove_staging(staging, staging_root)
        raise
    except Exception as exc:  # noqa: BLE001
        _remove_staging(staging, staging_root)
        raise ModelPullError("DOWNLOAD_OR_INSTALL_FAILED: %r" % (exc,)) from exc

    if target.exists():
        try:
            validate_model_manifest(
                target,
                expected_model_type=model_type,
                expected_version=target_version,
            )
        except ManifestValidationError:
            quarantine = target.with_name(
                "%s.corrupt-%d" % (target.name, time.time_ns())
            )
            target.replace(quarantine)
        else:
            _remove_staging(staging, staging_root)
            return {
                "action": "already_installed",
                "update_id": update_id,
                "model_type": model_type,
                "installed_version": target_version,
            }
    staging.replace(target)
    return {
        "action": "installed",
        "update_id": update_id,
        "model_type": model_type,
        "installed_version": target_version,
    }


def _unpack_bundle(payload: bytes, staging: Path) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ModelPullError(
                        "BUNDLE_PATH_ESCAPE=%s" % member.filename
                    )
                destination = staging.joinpath(*relative.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
    except zipfile.BadZipFile as exc:
        raise ModelPullError("INVALID_BUNDLE_ARCHIVE") from exc


def _remove_staging(staging: Path, staging_root: Path) -> None:
    if not staging.exists():
        return
    resolved = staging.resolve()
    root = staging_root.resolve()
    if root not in resolved.parents:
        raise ModelPullError("MODEL_STAGING_PATH_INVALID")
    shutil.rmtree(resolved)
