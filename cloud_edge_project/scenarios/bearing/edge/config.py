from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from .settings import ConstantDetectionConfig, PerceptionConfig, file_sha256


_CHANNELS = ("vibration", "phase_current_1_A", "phase_current_2_A")


def build_bearing_perception_config(
    environ: Mapping[str, str] | None = None,
) -> PerceptionConfig:
    """从环境变量构造轴承感知配置，并保留当前运行默认值。"""

    environment = os.environ if environ is None else environ
    default_fir = Path(__file__).resolve().parent / "assets" / "fir_64k_to_16k_369.txt"
    fir_path = Path(
        environment.get("EDGE_PERCEPTION_FIR_PATH", str(default_fir))
    ).expanduser().resolve()
    profile = environment.get("EDGE_PERCEPTION_PROFILE", "development_test")
    source = environment.get("EDGE_PERCEPTION_CONFIG_SOURCE", profile)
    version = environment.get("EDGE_PERCEPTION_CONFIG_VERSION", "runtime-v1")
    zero_rms = _float(
        environment,
        "EDGE_PERCEPTION_FEATURE_ZERO_RMS_THRESHOLD",
        1e-10,
    )
    return PerceptionConfig(
        profile=profile,
        fir_coefficients_path=fir_path,
        fir_sha256=environment.get(
            "EDGE_PERCEPTION_FIR_SHA256",
            file_sha256(fir_path),
        ),
        fir_asset_source=source,
        fir_asset_version=environment.get(
            "EDGE_PERCEPTION_FIR_VERSION",
            "bundled-v1",
        ),
        running_speed_threshold_rpm=_float(
            environment,
            "EDGE_PERCEPTION_RUNNING_SPEED_THRESHOLD_RPM",
            100.0,
        ),
        running_speed_threshold_source=source,
        running_speed_threshold_version=version,
        constant_detection={
            channel: ConstantDetectionConfig(
                enabled=_boolean(
                    environment,
                    f"EDGE_PERCEPTION_{channel.upper()}_CONSTANT_ENABLED",
                    True,
                ),
                threshold=_float(
                    environment,
                    f"EDGE_PERCEPTION_{channel.upper()}_CONSTANT_THRESHOLD",
                    _float(
                        environment,
                        "EDGE_PERCEPTION_CONSTANT_THRESHOLD",
                        1e-9,
                    ),
                ),
                source=source,
                version=version,
            )
            for channel in _CHANNELS
        },
        feature_zero_rms_threshold=zero_rms,
        feature_zero_power_threshold=_zero_power_threshold(environment, zero_rms),
        current_relationship_zero_rms_threshold=_float(
            environment,
            "EDGE_PERCEPTION_CURRENT_ZERO_RMS_THRESHOLD",
            1e-10,
        ),
        numerical_threshold_source=source,
        numerical_threshold_version=version,
        feature_extractor_version=environment.get(
            "EDGE_PERCEPTION_FEATURE_EXTRACTOR_VERSION",
            "edge-perception-v1",
        ),
        runtime_dependencies={"numpy": np.__version__},
        absolute_tolerance=_float(
            environment,
            "EDGE_PERCEPTION_ABSOLUTE_TOLERANCE",
            1e-12,
        ),
        relative_tolerance=_float(
            environment,
            "EDGE_PERCEPTION_RELATIVE_TOLERANCE",
            1e-9,
        ),
    )


def _float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _zero_power_threshold(
    environment: Mapping[str, str],
    zero_rms: float,
) -> float:
    if "EDGE_PERCEPTION_FEATURE_ZERO_POWER_THRESHOLD" in environment:
        return _float(
            environment,
            "EDGE_PERCEPTION_FEATURE_ZERO_POWER_THRESHOLD",
            1e-20,
        )
    if "EDGE_PERCEPTION_FEATURE_ZERO_RMS_THRESHOLD" in environment:
        return zero_rms**2
    return 1e-20


def _boolean(environment: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
