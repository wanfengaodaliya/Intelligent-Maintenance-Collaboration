# -*- coding: utf-8 -*-
"""持久化模型仓启动播种与初始版本选择。"""
from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .manifest_validation import ManifestValidationError, validate_model_manifest
from .version_store import (
    VersionStoreError,
    read_active_version,
    set_active_version,
    validate_path_component,
    version_dir,
)


MODEL_TYPE_DISTILLED_H5 = "distilled_h5"
MODEL_PIN_POLLER_CONFLICT = "EDGE_MODEL_PIN_POLLER_CONFLICT"


class ModelStoreBootstrapError(RuntimeError):
    """模型仓无法安全播种或选择初始版本。"""


@dataclass(frozen=True)
class ModelStoreSelection:
    model_root: Path
    version: str
    pinned: bool


def validate_model_update_mode(
    *, pinned_version: str | None, poller_enabled: bool
) -> None:
    """配置冲突检查；调用方必须在任何磁盘副作用前执行。"""
    if pinned_version and poller_enabled:
        raise ModelStoreBootstrapError(MODEL_PIN_POLLER_CONFLICT)
    if pinned_version:
        try:
            validate_path_component(pinned_version, field="MODEL_VERSION")
        except VersionStoreError as exc:
            raise ModelStoreBootstrapError(str(exc)) from exc


def initialize_model_store(
    *,
    model_root: Path | str,
    bundled_model_root: Path | str,
    baseline_version: str,
    pinned_version: str | None,
) -> ModelStoreSelection:
    """播种正式基线并选择已完整校验的初始版本。"""
    runtime_root = Path(model_root).resolve()
    bundled_root = Path(bundled_model_root).resolve()
    _seed_baseline(
        runtime_root=runtime_root,
        bundled_root=bundled_root,
        baseline_version=baseline_version,
    )

    if pinned_version:
        selected = pinned_version.strip()
        pinned = True
    else:
        pinned = False
        try:
            selected = read_active_version(
                MODEL_TYPE_DISTILLED_H5, base=runtime_root
            )
        except VersionStoreError as exc:
            raise ModelStoreBootstrapError(str(exc)) from exc
        if selected is None:
            set_active_version(
                MODEL_TYPE_DISTILLED_H5,
                baseline_version,
                base=runtime_root,
            )
            selected = baseline_version

    try:
        selected_dir = version_dir(
            MODEL_TYPE_DISTILLED_H5, selected, base=runtime_root
        )
        validate_model_manifest(selected_dir, expected_version=selected)
    except (ManifestValidationError, VersionStoreError) as exc:
        code = "MODEL_PIN_TARGET_INVALID" if pinned else "MODEL_ACTIVE_TARGET_INVALID"
        raise ModelStoreBootstrapError("%s: %s" % (code, exc)) from exc
    return ModelStoreSelection(runtime_root, selected, pinned)


def _seed_baseline(
    *, runtime_root: Path, bundled_root: Path, baseline_version: str
) -> None:
    source = version_dir(
        MODEL_TYPE_DISTILLED_H5, baseline_version, base=bundled_root
    ).resolve()
    try:
        validate_model_manifest(source, expected_version=baseline_version)
    except ManifestValidationError as exc:
        raise ModelStoreBootstrapError("MODEL_BUNDLED_BASELINE_INVALID: %s" % exc) from exc

    target = version_dir(
        MODEL_TYPE_DISTILLED_H5, baseline_version, base=runtime_root
    ).resolve()
    if source == target:
        return
    family_root = target.parent
    family_root.mkdir(parents=True, exist_ok=True)
    _cleanup_seed_staging(family_root, baseline_version)

    if target.exists():
        try:
            validate_model_manifest(target, expected_version=baseline_version)
        except ManifestValidationError:
            quarantine = target.with_name(
                "%s.corrupt-%d" % (target.name, time.time_ns())
            )
            target.replace(quarantine)
        else:
            return

    staging = family_root / (
        ".%s.seed-%s" % (baseline_version, uuid.uuid4().hex)
    )
    try:
        shutil.copytree(source, staging)
        validate_model_manifest(staging, expected_version=baseline_version)
        staging.replace(target)
    except Exception as exc:
        _remove_seed_dir(staging, family_root)
        if isinstance(exc, ModelStoreBootstrapError):
            raise
        raise ModelStoreBootstrapError("MODEL_BASELINE_SEED_FAILED: %s" % exc) from exc


def _cleanup_seed_staging(family_root: Path, baseline_version: str) -> None:
    prefix = ".%s.seed-" % baseline_version
    for entry in family_root.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            _remove_seed_dir(entry, family_root)


def _remove_seed_dir(path: Path, family_root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    root = family_root.resolve()
    if root not in resolved.parents:
        raise ModelStoreBootstrapError("MODEL_SEED_PATH_INVALID")
    shutil.rmtree(resolved)
