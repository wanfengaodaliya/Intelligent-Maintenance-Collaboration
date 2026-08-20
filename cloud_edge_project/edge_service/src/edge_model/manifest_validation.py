# -*- coding: utf-8 -*-
"""共享模型制品 manifest 完整性校验。

实现方案 5.4：下载解压、空卷播种、启动加载、热更新候选加载四个时机都必须复用
同一套完整校验，而非各自只校验 ``best_model.pt``。本模块只依赖 stdlib，供拉取
客户端（无 torch 环境）与蒸馏 H5 模型（torch 环境）共同引用。

校验范围：
- ``manifest.json`` 可解析；
- ``model_type`` / ``version`` 与目标目录一致；
- H5 运行所需最小文件齐全；
- manifest ``files`` 中所有相对路径不逃逸版本目录；
- ``files`` 全部条目存在且 SHA256 一致（不限制额外文件）；
- ``best_model.pt`` 与 ``checkpoint_sha256.txt`` 一致；
- 归一化与 schema 文件结构合法。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

MODEL_TYPE_DISTILLED_H5 = "distilled_h5"
H5_FEATURE_PIPELINE_VERSION = "edge_feature_v1"

# H5 前向推理必需文件（缺一即不可加载）。
H5_REQUIRED_FILES = (
    "best_model.pt",
    "checkpoint_sha256.txt",
    "physical_feature_normalization.json",
    "physical_feature_schema.json",
    "condition_norm.json",
    "condition_schema.json",
)


class ManifestValidationError(RuntimeError):
    """模型制品 manifest 完整性校验失败。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_model_manifest(
    model_dir: Path | str,
    *,
    expected_model_type: str = MODEL_TYPE_DISTILLED_H5,
    expected_version: str | None = None,
    expected_feature_pipeline_version: str = H5_FEATURE_PIPELINE_VERSION,
) -> dict[str, Any]:
    """校验一个版本目录的完整 manifest 并返回已解析的 manifest。

    任一校验失败抛出 :class:`ManifestValidationError`，错误码可区分具体原因。
    """
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise ManifestValidationError("MODEL_MANIFEST_DIR_MISSING")
    manifest_path = model_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("MODEL_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise ManifestValidationError("MODEL_MANIFEST_NOT_OBJECT")

    model_type = manifest.get("model_type")
    if model_type != expected_model_type:
        raise ManifestValidationError(
            "MODEL_MANIFEST_MODEL_TYPE_MISMATCH: expected=%s got=%s"
            % (expected_model_type, model_type)
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ManifestValidationError("MODEL_MANIFEST_VERSION_MISSING")
    if expected_version is not None and version != expected_version:
        raise ManifestValidationError(
            "MODEL_MANIFEST_VERSION_MISMATCH: expected=%s got=%s"
            % (expected_version, version)
        )
    feature_pipeline_version = manifest.get("feature_pipeline_version")
    if feature_pipeline_version != expected_feature_pipeline_version:
        raise ManifestValidationError(
            "MODEL_MANIFEST_FEATURE_PIPELINE_VERSION_MISMATCH: expected=%s got=%s"
            % (expected_feature_pipeline_version, feature_pipeline_version)
        )

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ManifestValidationError("MODEL_MANIFEST_FILES_MISSING")
    for required in H5_REQUIRED_FILES:
        if required not in files:
            raise ManifestValidationError(
                "MODEL_MANIFEST_MISSING_REQUIRED_FILE=%s" % required
            )

    root = model_dir.resolve()
    for rel_path, expected in files.items():
        if not isinstance(rel_path, str) or not rel_path:
            raise ManifestValidationError("MODEL_MANIFEST_FILE_PATH_INVALID")
        resolved = (model_dir / rel_path).resolve()
        if root not in resolved.parents:
            raise ManifestValidationError("MODEL_MANIFEST_PATH_ESCAPE=%s" % rel_path)
        if not resolved.is_file():
            raise ManifestValidationError("MODEL_MANIFEST_FILE_MISSING=%s" % rel_path)
        if not isinstance(expected, str) or len(expected) != 64:
            raise ManifestValidationError("MODEL_MANIFEST_FILE_HASH_INVALID=%s" % rel_path)
        if _sha256(resolved) != expected.lower():
            raise ManifestValidationError("MODEL_MANIFEST_SHA256_MISMATCH=%s" % rel_path)

    _validate_checkpoint(model_dir)
    _validate_normalization(model_dir / "physical_feature_normalization.json", expected_size=19)
    _validate_normalization(model_dir / "condition_norm.json", expected_size=13)
    _validate_schema(
        model_dir / "physical_feature_schema.json",
        names_key="feature_names",
        size_key="num_features",
        expected_size=19,
    )
    _validate_schema(
        model_dir / "condition_schema.json",
        names_key="fields",
        size_key="dim",
        expected_size=13,
    )
    return manifest


def _validate_checkpoint(model_dir: Path) -> None:
    checkpoint = model_dir / "best_model.pt"
    checksum_file = model_dir / "checkpoint_sha256.txt"
    try:
        expected = checksum_file.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise ManifestValidationError("MODEL_MANIFEST_CHECKPOINT_CHECKSUM_MISSING") from exc
    if len(expected) != 64 or _sha256(checkpoint) != expected:
        raise ManifestValidationError("MODEL_MANIFEST_CHECKPOINT_SHA256_MISMATCH")


def _validate_normalization(path: Path, *, expected_size: int) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        mean = value["mean"]
        std = value["std"]
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("MODEL_MANIFEST_NORMALIZATION_INVALID") from exc
    if not isinstance(mean, list) or not isinstance(std, list):
        raise ManifestValidationError("MODEL_MANIFEST_NORMALIZATION_INVALID")
    if len(mean) != expected_size or len(std) != expected_size:
        raise ManifestValidationError(
            "MODEL_MANIFEST_NORMALIZATION_SIZE_MISMATCH: %s" % path.name
        )
    if any(
        isinstance(x, bool)
        or not isinstance(x, (int, float))
        or not math.isfinite(x)
        for x in (*mean, *std)
    ):
        raise ManifestValidationError(
            "MODEL_MANIFEST_NORMALIZATION_NON_FINITE: %s" % path.name
        )
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or x <= 0 for x in std):
        raise ManifestValidationError(
            "MODEL_MANIFEST_NORMALIZATION_STD_INVALID: %s" % path.name
        )


def _validate_schema(path: Path, *, names_key: str, size_key: str, expected_size: int) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        names = value[names_key]
        size = value[size_key]
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("MODEL_MANIFEST_SCHEMA_INVALID") from exc
    if not isinstance(names, list) or len(names) != expected_size:
        raise ManifestValidationError(
            "MODEL_MANIFEST_SCHEMA_NAMES_INVALID: %s" % path.name
        )
    if size != expected_size:
        raise ManifestValidationError(
            "MODEL_MANIFEST_SCHEMA_SIZE_MISMATCH: %s" % path.name
        )
