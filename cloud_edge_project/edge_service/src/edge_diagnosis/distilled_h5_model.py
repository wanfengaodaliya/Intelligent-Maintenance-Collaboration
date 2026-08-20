"""Production runner for the distilled three-branch H5 bearing model."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import torch
from scipy.signal import resample_poly

from edge_model.code_fallback import CodeFallbackRunner
from edge_model.contracts import EdgeResult, InferenceCancelled, PacketInferenceTask
from edge_model.version_store import (
    resolve_active_version,
    version_dir,
)


EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[2]
from edge_diagnosis.h5_features import _compute_single, normalize_features
from edge_diagnosis.h5_network import PhysicalFusionModel


H5_LABELS = ("healthy", "outer_ring_damage", "inner_ring_damage")
# 基线默认版本：未配置 EDGE_MODEL_VERSION 且无激活指针时回退到该版本。
RUNTIME_MODEL_VERSION = "distilled_h5_kd_fold3_a9f20442"
DEFAULT_MODEL_DIR = EDGE_SERVICE_ROOT / "models" / "distilled_h5"


def _resolve_model_dir() -> Path:
    version = resolve_active_version(
        "distilled_h5", default_version=RUNTIME_MODEL_VERSION
    )
    return version_dir("distilled_h5", version)


def _resolve_model_version(model_dir: Path) -> str:
    manifest = model_dir / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return model_dir.name


class H5ModelArtifactError(RuntimeError):
    """Raised when the frozen H5 deployment artifacts are absent or inconsistent."""


class DistilledH5DiagnosticModel(CodeFallbackRunner):
    """Run the frozen 50 ms H5 model from a validated raw edge packet.

    ``model_dir`` points at a version directory under ``models/distilled_h5/``.
    When omitted, the active version is resolved from ``EDGE_MODEL_VERSION`` /
    ``active_version.json`` and defaults to the baseline version.
    """

    def __init__(
        self,
        model_dir: Path | str | None = None,
        *,
        device: str = "cpu",
        model_version: str | None = None,
    ) -> None:
        self.model_dir = Path(model_dir) if model_dir else _resolve_model_dir()
        self.checkpoint_path = self.model_dir / "best_model.pt"
        self.checksum_path = self.model_dir / "checkpoint_sha256.txt"
        physical_normalization_path = self.model_dir / "physical_feature_normalization.json"
        condition_normalization_path = self.model_dir / "condition_norm.json"
        self.device = torch.device(device)
        self._verify_checkpoint()
        self.physical_mean, self.physical_std = _normalization(
            physical_normalization_path, expected_size=19
        )
        self.condition_mean, self.condition_std = _normalization(
            condition_normalization_path, expected_size=13
        )
        self.model = PhysicalFusionModel(
            num_classes=3,
            phys_dim=19,
            cond_dim=13,
            cond_scale=0.25,
            cond_dropout=0.5,
            use_h4_cnn=False,
        )
        try:
            checkpoint = torch.load(
                self.checkpoint_path, map_location="cpu", weights_only=False
            )
            state = checkpoint["model_state_dict"]
            h5_state = {key[3:]: value for key, value in state.items() if key.startswith("h5.")}
            if not h5_state:
                raise KeyError("model_state_dict has no h5.* weights")
            self.model.load_state_dict(h5_state)
        except (OSError, KeyError, RuntimeError) as exc:
            raise H5ModelArtifactError("distilled H5 checkpoint cannot be loaded") from exc
        self.model.to(self.device)
        self.model.eval()
        resolved = model_version or _resolve_model_version(self.model_dir)
        self.rule_version = resolved
        self.model_version = resolved
        self.deployment_status = "production"

    def _verify_checkpoint(self) -> None:
        try:
            expected = self.checksum_path.read_text(encoding="utf-8").split()[0].lower()
            actual = _sha256(self.checkpoint_path)
        except (OSError, IndexError) as exc:
            raise H5ModelArtifactError("distilled H5 checkpoint checksum is unavailable") from exc
        if len(expected) != 64 or actual != expected:
            raise H5ModelArtifactError("distilled H5 checkpoint checksum mismatch")

    def prepare_inputs(
        self, raw_packet: Mapping[str, Any], cancel_event=None
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        data = _mapping(raw_packet, "data")
        vibration_64k = _series(data, "vibration", sample_rate=64_000, sample_count=3_200)
        vibration_16k = resample_poly(vibration_64k, up=1, down=4).astype(np.float32)
        if vibration_16k.shape != (800,):
            raise ValueError("vibration downsampling did not produce 800 samples")
        _check_cancelled(cancel_event)  # 检查点：物理特征计算前
        physical = normalize_features(
            _compute_single(vibration_64k), self.physical_mean, self.physical_std
        )
        _check_cancelled(cancel_event)  # 检查点：工况特征计算前
        condition = _condition_vector(data, self.condition_mean, self.condition_std)
        return (
            torch.from_numpy(vibration_16k[None, :]).to(self.device),
            torch.from_numpy(physical[None, :]).to(self.device),
            torch.from_numpy(condition[None, :]).to(self.device),
        )

    def run(self, task: PacketInferenceTask, cancel_event=None) -> EdgeResult:
        if task.raw_packet is None:
            raise ValueError("distilled H5 requires the validated raw packet")
        _check_cancelled(cancel_event)  # 检查点：推理入口
        vibration, physical, condition = self.prepare_inputs(
            task.raw_packet, cancel_event=cancel_event
        )
        _check_cancelled(cancel_event)  # 检查点：CNN/融合前向计算前
        with torch.no_grad():
            logits, _ = self.model(vibration, physical, condition)
            probabilities = torch.softmax(logits, dim=1).cpu().numpy()[0]
        if probabilities.shape != (3,) or not np.isfinite(probabilities).all():
            raise ValueError("distilled H5 probabilities are invalid")
        index = int(np.argmax(probabilities))
        label = H5_LABELS[index]
        probability_map = {
            name: round(float(probabilities[position]), 6)
            for position, name in enumerate(H5_LABELS)
        }
        fault = label != "healthy"
        result = EdgeResult(
            edge_result="fault" if fault else "normal",
            confidence=probability_map[label],
            edge_risk_level="high" if fault else "low",
            model_version=self.model_version,
            diagnosis_label=label,
            class_probabilities=probability_map,
        )
        self._validate_output(result)
        return result

    def build_evidence(self, raw_packet: Mapping[str, Any]) -> dict[str, Any]:
        """Build the existing cloud-review evidence contract without old perception."""
        data = _mapping(raw_packet, "data")
        vibration = _series(data, "vibration", sample_rate=64_000, sample_count=3_200)
        vibration_16k = resample_poly(vibration, up=1, down=4).astype(np.float32)
        current_1 = _series(data, "phase_current_1_A", sample_rate=64_000, sample_count=3_200)
        current_2 = _series(data, "phase_current_2_A", sample_rate=64_000, sample_count=3_200)
        current_1_16k = resample_poly(current_1, up=1, down=4).astype(np.float32)
        current_2_16k = resample_poly(current_2, up=1, down=4).astype(np.float32)
        current_1_features = _current_evidence(current_1_16k)
        current_2_features = _current_evidence(current_2_16k)
        current_mean_rms = (current_1_features["rms_a"] + current_2_features["rms_a"]) / 2.0
        imbalance = (
            0.0 if current_mean_rms <= 1e-20
            else abs(current_1_features["rms_a"] - current_2_features["rms_a"]) / current_mean_rms
        )
        speed = _series(data, "shaft_speed_rpm", sample_rate=4_000, sample_count=200)
        torque = _series(data, "load_torque_nm", sample_rate=4_000, sample_count=200)
        force = _series(data, "bearing_radial_load_n", sample_rate=4_000, sample_count=200)
        temperature = data.get("bearing_module_temperature_c")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not np.isfinite(temperature):
            raise ValueError("bearing_module_temperature_c must be finite")
        end_ns = raw_packet.get("end_generate_timestamp_ns")
        if isinstance(end_ns, bool) or not isinstance(end_ns, int) or end_ns <= 0:
            raise ValueError("end_generate_timestamp_ns must be positive")
        identity = {
            name: raw_packet.get(name)
            for name in ("device_id", "bearing_id", "task_id", "packet_id", "sender_id")
        }
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ValueError("raw packet identity is invalid")
        sequence = raw_packet.get("sequence_number")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("raw packet sequence_number is invalid")
        return {
            **identity,
            "sequence_number": sequence,
            "end_generate_timestamp_ns": end_ns,
            "feature_generated_at_ns": max(end_ns, time.time_ns()),
            "perception_quality": {"status": "good", "flags": []},
            "features": {
                "vibration": _vibration_evidence(vibration_16k),
                "phase_current_1": current_1_features,
                "phase_current_2": current_2_features,
                "current_relationship": {"current_imbalance_ratio": float(imbalance)},
                "operating_context": {
                    "shaft_speed_rpm": _statistics(speed),
                    "load_torque_nm": _statistics(torque),
                    "bearing_radial_load_n": _statistics(force),
                    "bearing_module_temperature_c": float(temperature),
                },
            },
        }


def _check_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InferenceCancelled("distilled H5 inference cancelled")


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ValueError("raw packet %s must be an object" % name)
    return result


def _series(data: Mapping[str, Any], name: str, *, sample_rate: int, sample_count: int) -> np.ndarray:
    source = _mapping(data, name)
    if source.get("sample_rate_hz") != sample_rate or source.get("sample_count") != sample_count:
        raise ValueError("%s must be %d Hz / %d samples" % (name, sample_rate, sample_count))
    try:
        values = np.asarray(source["values"], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("%s values must be numeric" % name) from exc
    if values.shape != (sample_count,) or not np.isfinite(values).all():
        raise ValueError("%s values must be finite %d-sample data" % (name, sample_count))
    return values


def _condition_vector(data: Mapping[str, Any], mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    values: list[float] = []
    for name in ("shaft_speed_rpm", "load_torque_nm", "bearing_radial_load_n"):
        series = _series(data, name, sample_rate=4_000, sample_count=200).astype(np.float64)
        values.extend((float(series.mean()), float(series.std()), float(series.min()), float(series.max())))
    temperature = data.get("bearing_module_temperature_c")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not np.isfinite(temperature):
        raise ValueError("bearing_module_temperature_c must be finite")
    values.append(float(temperature))
    return ((np.asarray(values, dtype=np.float32) - mean) / std).astype(np.float32)


def _normalization(path: Path | str, *, expected_size: int) -> tuple[np.ndarray, np.ndarray]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        mean = np.asarray(value["mean"], dtype=np.float32)
        std = np.asarray(value["std"], dtype=np.float32)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise H5ModelArtifactError("H5 normalization metadata is invalid") from exc
    if mean.shape != (expected_size,) or std.shape != (expected_size,) or not np.isfinite(mean).all() or np.any(std <= 0):
        raise H5ModelArtifactError("H5 normalization metadata has invalid dimensions")
    return mean, std


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _statistics(values: np.ndarray) -> dict[str, float]:
    return {
        "mean": float(np.mean(values, dtype=np.float64)),
        "last": float(values[-1]),
        "minimum": float(np.min(values)),
        "maximum": float(np.max(values)),
        "standard_deviation": float(np.std(values, dtype=np.float64)),
    }


def _current_evidence(values: np.ndarray) -> dict[str, float | int | str]:
    centered = values - np.mean(values, dtype=np.float64)
    return {
        "source_sample_rate_hz": 64_000,
        "analysis_sample_rate_hz": 16_000,
        "rms_a": float(np.sqrt(np.mean(centered * centered, dtype=np.float64))),
        "absolute_peak_a": float(np.max(np.abs(centered))),
        "unit": "A",
    }


def _vibration_evidence(values: np.ndarray) -> dict[str, float | int | str]:
    centered = values - np.mean(values, dtype=np.float64)
    power = np.abs(np.fft.rfft(centered)) ** 2
    frequencies = np.fft.rfftfreq(centered.size, d=1.0 / 16_000)
    non_dc_power = power[1:]
    total = float(np.sum(non_dc_power))
    if total <= 1e-20:
        dominant_frequency, band_ratio, entropy, kurtosis = 20.0, 0.0, 0.0, 0.0
    else:
        dominant_frequency = float(frequencies[1 + int(np.argmax(non_dc_power))])
        band = (frequencies >= 500.0) & (frequencies <= 2_000.0)
        band_ratio = float(np.sum(power[band]) / total)
        probabilities = non_dc_power / total
        positive = probabilities > 0
        entropy = float(-np.sum(probabilities[positive] * np.log(probabilities[positive])) / np.log(non_dc_power.size))
        standard_deviation = float(np.std(centered, dtype=np.float64))
        kurtosis = 0.0 if standard_deviation <= 1e-20 else float(np.mean((centered / standard_deviation) ** 4) - 3.0)
    return {
        "source_sample_rate_hz": 64_000,
        "analysis_sample_rate_hz": 16_000,
        "rms": float(np.sqrt(np.mean(centered * centered, dtype=np.float64))),
        "absolute_peak": float(np.max(np.abs(centered))),
        "kurtosis": kurtosis,
        "dominant_frequency_hz": max(20.0, min(8_000.0, dominant_frequency)),
        "band_power_ratio_500_2000": max(0.0, min(1.0, band_ratio)),
        "spectral_entropy": max(0.0, min(1.0, entropy)),
        "unit": "mm/s",
    }
