# -*- coding: utf-8 -*-
"""固化 H5 运行时探针加载与标准 ``raw_packet`` 适配。

实现方案 6：从 ``resources/model_probes/distilled_h5/v1`` 读取 ``probe.npz`` +
``manifest.json``，执行"manifest 声明 + NPZ 实际内容 + H5 代码合同"三层一致性
校验，再把裸通道数组重建为标准 ``raw_packet`` 与隔离的 ``PacketInferenceTask``，
供候选模型热更新门禁调用 ``candidate_model.run(task)``。

本模块只依赖 numpy 与 Edge 内部合同，不依赖 ``sender_module``、torch 或源
``.mat`` 文件。探针任务使用固定探针专用标识，不进入正常任务登记、运行队列、
业务统计或结果发布。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import PacketInferenceTask


MODEL_TYPE_DISTILLED_H5 = "distilled_h5"
H5_FEATURE_PIPELINE_VERSION = "edge_feature_v1"

# H5 ``run()`` 代码合同：振动必须 64000/3200，三路工况必须 4000/200。
_PROBE_CHANNEL_CONTRACT: dict[str, tuple[int, int]] = {
    "vibration": (64_000, 3_200),
    "shaft_speed_rpm": (4_000, 200),
    "load_torque_nm": (4_000, 200),
    "bearing_radial_load_n": (4_000, 200),
}
_TEMPERATURE_CHANNEL = "bearing_module_temperature_c"

_PROBE_DEVICE_ID = "__h5_probe_device__"
_PROBE_BEARING_ID = "__h5_probe_bearing__"
_PROBE_TASK_ID = "__h5_probe_task__"
_PROBE_PACKET_ID = "__h5_probe_packet__"
_PROBE_SENDER_ID = "__h5_probe_sender__"
_PROBE_REQUEST_ID = "__h5_probe_request__"
_PROBE_END_GENERATE_TIMESTAMP_NS = 1_050_000_000


class H5ProbeError(RuntimeError):
    """探针资源缺失、损坏或合同不一致。"""


def default_probe_dir() -> Path:
    """正式固化探针目录（edge_service/resources/model_probes/distilled_h5/v1）。"""
    return (
        Path(__file__).resolve().parents[2]
        / "resources"
        / "model_probes"
        / "distilled_h5"
        / "v1"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_probe_manifest(
    probe_dir: Path | str,
    *,
    expected_feature_pipeline_version: str = H5_FEATURE_PIPELINE_VERSION,
) -> dict[str, Any]:
    """校验探针 manifest 结构并返回已解析内容（不加载 NPZ）。

    校验：manifest 可解析、``model_type`` 正确、``feature_pipeline_version`` 与
    期望一致、``artifact`` 声明了 ``file_name``/``sha256``/``channels``。
    """
    probe_dir = Path(probe_dir)
    manifest_path = probe_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise H5ProbeError("PROBE_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise H5ProbeError("PROBE_MANIFEST_NOT_OBJECT")

    if manifest.get("model_type") != MODEL_TYPE_DISTILLED_H5:
        raise H5ProbeError(
            "PROBE_MODEL_TYPE_MISMATCH: got=%s" % manifest.get("model_type")
        )
    if manifest.get("feature_pipeline_version") != expected_feature_pipeline_version:
        raise H5ProbeError(
            "PROBE_FEATURE_PIPELINE_VERSION_MISMATCH: expected=%s got=%s"
            % (expected_feature_pipeline_version, manifest.get("feature_pipeline_version"))
        )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise H5ProbeError("PROBE_ARTIFACT_MISSING")
    if not isinstance(artifact.get("file_name"), str) or not artifact["file_name"]:
        raise H5ProbeError("PROBE_ARTIFACT_FILE_NAME_INVALID")
    sha256 = artifact.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise H5ProbeError("PROBE_ARTIFACT_SHA256_INVALID")
    channels = artifact.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise H5ProbeError("PROBE_ARTIFACT_CHANNELS_MISSING")
    return manifest


def load_h5_probe(
    probe_dir: Path | str,
    *,
    expected_feature_pipeline_version: str = H5_FEATURE_PIPELINE_VERSION,
) -> tuple[PacketInferenceTask, dict[str, Any]]:
    """加载并校验探针，返回 ``(隔离任务, 探针 manifest)``。

    校验顺序：manifest 结构 → NPZ 文件存在与 SHA256 → 各通道 key/dtype/shape/
    有限数值与 H5 代码合同一致 → 重建标准 raw_packet。任何一层不一致都在候选
    模型前向传播前抛出 :class:`H5ProbeError`。
    """
    probe_dir = Path(probe_dir)
    manifest = read_probe_manifest(
        probe_dir, expected_feature_pipeline_version=expected_feature_pipeline_version
    )
    artifact = manifest["artifact"]
    root = probe_dir.resolve()
    npz_path = (probe_dir / artifact["file_name"]).resolve()
    if root not in npz_path.parents:
        raise H5ProbeError("PROBE_ARTIFACT_PATH_ESCAPE")
    if not npz_path.is_file():
        raise H5ProbeError("PROBE_NPZ_MISSING")
    if _sha256(npz_path) != str(artifact["sha256"]).lower():
        raise H5ProbeError("PROBE_SHA256_MISMATCH")

    try:
        with np.load(npz_path, allow_pickle=False) as data:
            arrays = {name: data[name] for name in data.files}
    except Exception as exc:  # noqa: BLE001
        raise H5ProbeError("PROBE_NPZ_LOAD_FAILED: %r" % (exc,)) from exc

    channels_meta = artifact["channels"]
    raw_packet = _build_raw_packet(channels_meta, arrays)
    task = PacketInferenceTask(
        request_id=_PROBE_REQUEST_ID,
        device_id=_PROBE_DEVICE_ID,
        bearing_id=_PROBE_BEARING_ID,
        task_id=_PROBE_TASK_ID,
        packet_id=_PROBE_PACKET_ID,
        sender_id=_PROBE_SENDER_ID,
        sequence_number=1,
        perception={},
        raw_packet=raw_packet,
    )
    return task, manifest


def load_h5_probe_task(
    probe_dir: Path | str,
    *,
    expected_feature_pipeline_version: str = H5_FEATURE_PIPELINE_VERSION,
) -> PacketInferenceTask:
    """加载探针并返回隔离的 ``PacketInferenceTask``（方案 6.3 唯一适配入口）。"""
    task, _ = load_h5_probe(
        probe_dir, expected_feature_pipeline_version=expected_feature_pipeline_version
    )
    return task


def _as_float32_array(name: str, value: np.ndarray) -> np.ndarray:
    array = np.asarray(value)
    if array.dtype != np.dtype("float32"):
        raise H5ProbeError("PROBE_CHANNEL_DTYPE_INVALID=%s" % name)
    if not np.isfinite(array).all():
        raise H5ProbeError("PROBE_CHANNEL_NON_FINITE=%s" % name)
    return array


def _build_raw_packet(
    channels_meta: dict[str, Any], arrays: dict[str, np.ndarray]
) -> dict[str, Any]:
    if set(arrays) != set(channels_meta):
        missing = sorted(set(channels_meta) - set(arrays))
        extra = sorted(set(arrays) - set(channels_meta))
        raise H5ProbeError(
            "PROBE_NPZ_KEYS_MISMATCH: missing=%s extra=%s" % (missing, extra)
        )
    data: dict[str, Any] = {}
    for name, meta in channels_meta.items():
        if not isinstance(meta, dict):
            raise H5ProbeError("PROBE_CHANNEL_META_INVALID=%s" % name)
        if name not in arrays:
            raise H5ProbeError("PROBE_NPZ_KEY_MISSING=%s" % name)
        expected_dtype = meta.get("dtype")
        if expected_dtype != "float32":
            raise H5ProbeError("PROBE_CHANNEL_DTYPE_UNEXPECTED=%s" % name)
        expected_shape = meta.get("shape")
        if not isinstance(expected_shape, list):
            raise H5ProbeError("PROBE_CHANNEL_SHAPE_INVALID=%s" % name)
        array = _as_float32_array(name, arrays[name])
        if list(array.shape) != expected_shape:
            raise H5ProbeError(
                "PROBE_CHANNEL_SHAPE_MISMATCH=%s: expected=%s got=%s"
                % (name, expected_shape, list(array.shape))
            )
        if name == _TEMPERATURE_CHANNEL:
            if expected_shape != []:
                raise H5ProbeError("PROBE_TEMPERATURE_SHAPE_INVALID")
            data[name] = float(array)
            continue
        contract = _PROBE_CHANNEL_CONTRACT.get(name)
        if contract is None:
            raise H5ProbeError("PROBE_CHANNEL_UNKNOWN=%s" % name)
        contract_sample_rate_hz, contract_sample_count = contract
        declared_sample_rate_hz = meta.get("sample_rate_hz")
        if declared_sample_rate_hz != contract_sample_rate_hz:
            raise H5ProbeError(
                "PROBE_CHANNEL_SAMPLE_RATE_MISMATCH=%s" % name
            )
        actual_sample_count = int(array.shape[0]) if array.ndim == 1 else -1
        if actual_sample_count != contract_sample_count:
            raise H5ProbeError(
                "PROBE_CHANNEL_SAMPLE_COUNT_MISMATCH=%s: expected=%d got=%s"
                % (name, contract_sample_count, list(array.shape))
            )
        data[name] = {
            "sample_rate_hz": declared_sample_rate_hz,
            "sample_count": actual_sample_count,
            "values": array.tolist(),
        }

    for name in _PROBE_CHANNEL_CONTRACT:
        if name not in data:
            raise H5ProbeError("PROBE_CHANNEL_MISSING=%s" % name)
    if _TEMPERATURE_CHANNEL not in data:
        raise H5ProbeError("PROBE_TEMPERATURE_MISSING")

    return {
        "device_id": _PROBE_DEVICE_ID,
        "bearing_id": _PROBE_BEARING_ID,
        "task_id": _PROBE_TASK_ID,
        "packet_id": _PROBE_PACKET_ID,
        "sender_id": _PROBE_SENDER_ID,
        "sequence_number": 1,
        "end_generate_timestamp_ns": _PROBE_END_GENERATE_TIMESTAMP_NS,
        "data": data,
    }
