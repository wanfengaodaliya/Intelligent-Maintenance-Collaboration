# -*- coding: utf-8 -*-
"""边缘模型拉取客户端：从云端下载候选模型并原子激活新版本。

流程：下载 bundle（zip 或单文件）→ 暂存目录 → 逐文件 SHA256 校验 →
checkpoint 校验 → 移入版本目录 → 切换激活指针。任一步失败都保留旧版本
不变，回滚只需把激活指针切回上一版本。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .version_store import set_active_version, version_dir

_ZIP_MAGIC = b"PK\x03\x04"


class ModelPullError(RuntimeError):
    """Raised when a model bundle cannot be downloaded, verified or activated."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def pull_and_activate(
    *,
    update_id: str,
    download_url: str,
    target_version: str,
    model_type: str = "distilled_h5",
    model_root: Path | str | None = None,
    expected_sha256: str | None = None,
    http_get: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """下载候选模型并激活为目标版本。

    ``http_get`` 可注入以在测试中替换真实网络下载。返回激活结果描述。
    """
    fetch = http_get or _default_http_get
    root = Path(model_root) if model_root else None
    staging = version_dir(model_type, ".staging", base=root) / update_id
    target = version_dir(model_type, target_version, base=root)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        payload = fetch(download_url)
    except Exception as exc:  # noqa: BLE001
        raise ModelPullError("DOWNLOAD_FAILED: %r" % (exc,)) from exc

    if payload.startswith(_ZIP_MAGIC):
        _unpack_bundle(payload, staging, expected_sha256=expected_sha256)
    else:
        _store_single_file(payload, staging, expected_sha256=expected_sha256)

    _validate_checkpoint(staging)
    if target.exists():
        shutil.rmtree(target)
    staging.replace(target)
    set_active_version(model_type, target_version, base=root)
    return {
        "action": "activated",
        "update_id": update_id,
        "model_type": model_type,
        "activated_version": target_version,
    }


def rollback_version(
    *,
    model_type: str,
    target_version: str,
    model_root: Path | str | None = None,
) -> dict[str, Any]:
    """把激活指针回退到已安装的上一版本（物理回退）。

    与云端 ``execute_rollback`` 呼应：云端回退激活版本指针后，边端调用本
    函数把本地指针切回旧版本，推理路径据此加载旧制品。
    """
    root = Path(model_root) if model_root else None
    target = version_dir(model_type, target_version, base=root)
    if not (target / "manifest.json").is_file():
        raise ModelPullError("ROLLBACK_VERSION_NOT_INSTALLED")
    set_active_version(model_type, target_version, base=root)
    return {
        "action": "rolled_back",
        "model_type": model_type,
        "rolled_back_version": target_version,
    }


def _unpack_bundle(
    payload: bytes, staging: Path, *, expected_sha256: str | None
) -> None:
    try:
        with zipfile.ZipFile(io_bytes(payload)) as archive:
            archive.extractall(staging)
    except zipfile.BadZipFile as exc:
        raise ModelPullError("INVALID_BUNDLE_ARCHIVE") from exc
    manifest_path = staging / "manifest.json"
    if not manifest_path.is_file():
        raise ModelPullError("BUNDLE_MANIFEST_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ModelPullError("BUNDLE_MANIFEST_INVALID") from exc
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ModelPullError("BUNDLE_MANIFEST_NO_FILES")
    for rel_path, expected in files.items():
        resolved = (staging / rel_path).resolve()
        if staging not in resolved.parents or not resolved.is_file():
            raise ModelPullError("BUNDLE_FILE_MISSING=%s" % rel_path)
        if _sha256(resolved) != str(expected).lower():
            raise ModelPullError("BUNDLE_FILE_SHA256_MISMATCH=%s" % rel_path)
    if expected_sha256 and manifest.get("version"):
        # 主制品（best_model.pt）摘要与分发契约对齐（若契约提供）。
        primary = manifest.get("files", {}).get("best_model.pt")
        if primary and primary.lower() != expected_sha256.lower():
            raise ModelPullError("BUNDLE_PRIMARY_SHA256_MISMATCH")


def _store_single_file(
    payload: bytes, staging: Path, *, expected_sha256: str | None
) -> None:
    if expected_sha256:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256.lower():
            raise ModelPullError("SINGLE_FILE_SHA256_MISMATCH")
    (staging / "artifact.bin").write_bytes(payload)


def _validate_checkpoint(staging: Path) -> None:
    """轻量校验：best_model.pt 与 checkpoint_sha256.txt 一致（不加载 torch）。"""
    checkpoint = staging / "best_model.pt"
    checksum_file = staging / "checkpoint_sha256.txt"
    if not checkpoint.is_file():
        return
    if not checksum_file.is_file():
        raise ModelPullError("CHECKPOINT_CHECKSUM_MISSING")
    try:
        expected = checksum_file.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise ModelPullError("CHECKPOINT_CHECKSUM_INVALID") from exc
    if len(expected) != 64 or _sha256(checkpoint) != expected:
        raise ModelPullError("CHECKPOINT_SHA256_MISMATCH")


def io_bytes(payload: bytes) -> Any:
    import io

    return io.BytesIO(payload)
