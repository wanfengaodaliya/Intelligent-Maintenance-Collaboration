# -*- coding: utf-8 -*-
"""边缘模型版本目录管理与激活版本指针。

每个模型族在 ``models/<model_type>/`` 下按版本建子目录，``active_version.json``
指针选择运行时加载的版本；``EDGE_MODEL_VERSION`` 环境变量可显式覆盖指针，
用于部署时的版本 pin。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ROOT = Path(__file__).resolve().parents[2] / "models"


def model_root(*, base: Path | str | None = None) -> Path:
    return Path(base) if base else DEFAULT_MODEL_ROOT


def active_pointer_path(model_type: str, *, base: Path | str | None = None) -> Path:
    return model_root(base=base) / model_type / "active_version.json"


def version_dir(
    model_type: str, version: str, *, base: Path | str | None = None
) -> Path:
    return model_root(base=base) / model_type / version


def resolve_active_version(
    model_type: str,
    *,
    base: Path | str | None = None,
    env_override: str | None = None,
    default_version: str,
) -> str:
    """解析激活版本：环境变量覆盖 > 指针文件 > 默认版本。"""
    if env_override:
        return env_override.strip()
    pointer = active_pointer_path(model_type, base=base)
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return default_version


def set_active_version(
    model_type: str, version: str, *, base: Path | str | None = None
) -> Path:
    """原子切换模型族的激活版本指针（先写临时文件再 replace）。"""
    pointer = active_pointer_path(model_type, base=base)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": version, "updated_at_ns": time.time_ns()}
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(pointer)
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
