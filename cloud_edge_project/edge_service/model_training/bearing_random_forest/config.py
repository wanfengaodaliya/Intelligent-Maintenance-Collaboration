from __future__ import annotations

from pathlib import Path

import numpy as np

from cloud_edge_project.edge_service.src.edge_perception.config import (
    ConstantDetectionConfig,
    PerceptionConfig,
    file_sha256,
)


FEATURE_EXTRACTOR_VERSION = "edge-perception-rf-training-v1"


def training_perception_config() -> PerceptionConfig:
    source = "development_test"
    version = "rf-training-v1"
    fir_path = (
        Path(__file__).resolve().parents[2]
        / "edge_service"
        / "src"
        / "edge_perception"
        / "assets"
        / "fir_64k_to_16k_369.txt"
    )
    return PerceptionConfig(
        profile="development_test",
        fir_coefficients_path=fir_path,
        fir_sha256=file_sha256(fir_path),
        fir_asset_source=source,
        fir_asset_version="fir-64k-to-16k-369-v1",
        running_speed_threshold_rpm=100.0,
        running_speed_threshold_source=source,
        running_speed_threshold_version=version,
        constant_detection={
            "vibration": ConstantDetectionConfig(True, 1e-9, source, version),
            "phase_current_1_A": ConstantDetectionConfig(True, 1e-9, source, version),
            "phase_current_2_A": ConstantDetectionConfig(True, 1e-9, source, version),
        },
        feature_zero_rms_threshold=1e-10,
        feature_zero_power_threshold=1e-20,
        current_relationship_zero_rms_threshold=1e-10,
        numerical_threshold_source=source,
        numerical_threshold_version=version,
        feature_extractor_version=FEATURE_EXTRACTOR_VERSION,
        runtime_dependencies={"numpy": np.__version__},
        absolute_tolerance=1e-12,
        relative_tolerance=1e-9,
    )
