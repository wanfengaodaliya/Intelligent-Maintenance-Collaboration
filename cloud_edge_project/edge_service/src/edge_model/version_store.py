# -*- coding: utf-8 -*-
"""边缘模型版本目录管理与激活版本指针。

每个模型族在 ``models/<model_type>/`` 下按版本建子目录，``active_version.json``
指针选择运行时加载的版本；``EDGE_MODEL_VERSION`` 环境变量可显式覆盖指针，
用于部署时的版本 pin。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ROOT = Path(__file__).resolve().parents[2] / "models"


class VersionStoreError(RuntimeError):
    """活动版本指针缺失或格式损坏。"""


_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_path_component(value: str, *, field: str) -> str:
    """校验来自配置或 Cloud 的模型路径单段标识。"""
    if not isinstance(value, str):
        raise VersionStoreError("%s_INVALID" % field)
    normalized = value.strip()
    if not normalized or not _PATH_COMPONENT.fullmatch(normalized):
        raise VersionStoreError("%s_INVALID" % field)
    return normalized


def model_root(*, base: Path | str | None = None) -> Path:
    return Path(base) if base else DEFAULT_MODEL_ROOT


def active_pointer_path(model_type: str, *, base: Path | str | None = None) -> Path:
    model_type = validate_path_component(model_type, field="MODEL_TYPE")
    return model_root(base=base) / model_type / "active_version.json"


def version_dir(
    model_type: str, version: str, *, base: Path | str | None = None
) -> Path:
    model_type = validate_path_component(model_type, field="MODEL_TYPE")
    if version == ".staging":
        normalized_version = version
    else:
        normalized_version = validate_path_component(
            version, field="MODEL_VERSION"
        )
    return model_root(base=base) / model_type / normalized_version


def resolve_active_version(
    model_type: str,
    *,
    base: Path | str | None = None,
    env_override: str | None = None,
    default_version: str,
) -> str:
    """解析激活版本：环境变量覆盖 > 指针文件 > 默认版本。"""
    if env_override:
        return validate_path_component(env_override, field="MODEL_VERSION")
    pointer = active_pointer_path(model_type, base=base)
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return validate_path_component(version, field="MODEL_VERSION")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return default_version


def read_active_version(
    model_type: str, *, base: Path | str | None = None
) -> str | None:
    """严格读取活动版本；指针不存在返回 ``None``，损坏则抛出错误。"""
    pointer = active_pointer_path(model_type, base=base)
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise VersionStoreError("MODEL_ACTIVE_POINTER_INVALID") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise VersionStoreError("MODEL_ACTIVE_POINTER_VERSION_INVALID")
    return validate_path_component(version, field="MODEL_VERSION")


def set_active_version(
    model_type: str, version: str, *, base: Path | str | None = None
) -> Path:
    """原子切换模型族的激活版本指针（先写临时文件再 replace）。"""
    version = validate_path_component(version, field="MODEL_VERSION")
    pointer = active_pointer_path(model_type, base=base)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "updated_at_ns": time.time_ns()}
    tmp = pointer.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, pointer)
    if read_active_version(model_type, base=base) != version:
        raise VersionStoreError("MODEL_ACTIVE_POINTER_WRITE_VERIFY_FAILED")
    return pointer


def list_versions(
    model_type: str, *, base: Path | str | None = None
) -> list[str]:
    """列出带 manifest.json 的已就绪版本目录。"""
    root = model_root(base=base) / model_type
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "manifest.json").is_file()
    )


def read_manifest(
    model_type: str, version: str, *, base: Path | str | None = None
) -> dict[str, Any]:
    path = version_dir(model_type, version, base=base) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))
