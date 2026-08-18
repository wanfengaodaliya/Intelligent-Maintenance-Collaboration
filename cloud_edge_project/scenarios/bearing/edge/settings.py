# -*- coding: utf-8 -*-
"""感知模块显式配置；不提供业务阈值默认值。"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isclose, isfinite
from pathlib import Path
from typing import Mapping, Optional


PROFILE_DEVELOPMENT_TEST = "development_test"
PROFILE_PRODUCTION = "production"
_PROFILES = {PROFILE_DEVELOPMENT_TEST, PROFILE_PRODUCTION}
_CONSTANT_CHANNELS = {
    "vibration",
    "phase_current_1_A",
    "phase_current_2_A",
}


def file_sha256(path: Path | str) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ConstantDetectionConfig:
    enabled: bool
    threshold: Optional[float] = None
    source: Optional[str] = None
    version: Optional[str] = None


@dataclass(frozen=True)
class PerceptionConfig:
    profile: str
    fir_coefficients_path: Path
    fir_sha256: str
    fir_asset_source: str
    fir_asset_version: str
    running_speed_threshold_rpm: float
    running_speed_threshold_source: str
    running_speed_threshold_version: str
    constant_detection: Mapping[str, ConstantDetectionConfig]
    feature_zero_rms_threshold: float
    feature_zero_power_threshold: float
    current_relationship_zero_rms_threshold: float
    numerical_threshold_source: str
    numerical_threshold_version: str
    feature_extractor_version: str
    runtime_dependencies: Mapping[str, str]
    absolute_tolerance: float
    relative_tolerance: float

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.profile not in _PROFILES:
            errors.append("profile 只允许 development_test 或 production")
        if not Path(self.fir_coefficients_path).is_file():
            errors.append("FIR系数资产不存在")
        if len(self.fir_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in self.fir_sha256):
            errors.append("fir_sha256 必须是64位十六进制SHA-256")
        _require_source_version(
            errors, "fir_asset", self.fir_asset_source, self.fir_asset_version
        )

        if not _finite_non_bool(self.running_speed_threshold_rpm) or self.running_speed_threshold_rpm < 0:
            errors.append("running_speed_threshold_rpm 必须是有限非负数")
        _require_source_version(
            errors,
            "running_speed_threshold",
            self.running_speed_threshold_source,
            self.running_speed_threshold_version,
        )

        if set(self.constant_detection) != _CONSTANT_CHANNELS:
            errors.append("constant_detection 必须且只能配置三路高频通道")
        for channel in sorted(_CONSTANT_CHANNELS):
            rule = self.constant_detection.get(channel)
            if rule is None:
                continue
            if rule.enabled:
                if not _finite_non_bool(rule.threshold) or rule.threshold < 0:
                    errors.append(f"{channel}.constant_threshold 必须是有限非负数")
                _require_source_version(errors, f"{channel}.constant_threshold", rule.source, rule.version)

        if not _finite_non_bool(self.feature_zero_rms_threshold) or self.feature_zero_rms_threshold <= 0:
            errors.append("feature_zero_rms_threshold 必须是有限正数")
        if not _finite_non_bool(self.feature_zero_power_threshold) or self.feature_zero_power_threshold <= 0:
            errors.append("feature_zero_power_threshold 必须是有限正数")
        elif _finite_non_bool(self.feature_zero_rms_threshold) and not isclose(
            self.feature_zero_power_threshold,
            self.feature_zero_rms_threshold ** 2,
            rel_tol=max(float(self.relative_tolerance), 1e-12),
            abs_tol=max(float(self.absolute_tolerance), 1e-18),
        ):
            errors.append("feature_zero_power_threshold 必须等于 feature_zero_rms_threshold²")
        if (
            not _finite_non_bool(self.current_relationship_zero_rms_threshold)
            or self.current_relationship_zero_rms_threshold < 0
        ):
            errors.append("current_relationship_zero_rms_threshold 必须是有限非负数")
        _require_source_version(
            errors,
            "numerical_threshold",
            self.numerical_threshold_source,
            self.numerical_threshold_version,
        )

        if not isinstance(self.feature_extractor_version, str) or not self.feature_extractor_version.strip():
            errors.append("feature_extractor_version 必须是非空字符串")
        if not self.runtime_dependencies.get("numpy"):
            errors.append("runtime_dependencies 必须记录numpy版本")
        if not _finite_non_bool(self.absolute_tolerance) or self.absolute_tolerance <= 0:
            errors.append("absolute_tolerance 必须是有限正数")
        if not _finite_non_bool(self.relative_tolerance) or self.relative_tolerance < 0:
            errors.append("relative_tolerance 必须是有限非负数")

        if self.profile == PROFILE_DEVELOPMENT_TEST:
            for name, source in (
                ("fir_asset", self.fir_asset_source),
                ("running_speed_threshold", self.running_speed_threshold_source),
                ("numerical_threshold", self.numerical_threshold_source),
            ):
                if source != PROFILE_DEVELOPMENT_TEST:
                    errors.append(f"{name}.source 在开发测试模式下必须为 development_test")
            for channel, rule in self.constant_detection.items():
                if rule.enabled and rule.source != PROFILE_DEVELOPMENT_TEST:
                    errors.append(f"{channel}.constant_threshold.source 必须为 development_test")
        elif self.profile == PROFILE_PRODUCTION:
            sources = [
                self.fir_asset_source,
                self.running_speed_threshold_source,
                self.numerical_threshold_source,
            ]
            sources.extend(
                rule.source for rule in self.constant_detection.values() if rule.enabled
            )
            if PROFILE_DEVELOPMENT_TEST in sources:
                errors.append("production 模式禁止加载 development_test 来源的阈值")
        return errors


def _finite_non_bool(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(float(value))


def _require_source_version(
    errors: list[str], name: str, source: Optional[str], version: Optional[str]
) -> None:
    if not isinstance(source, str) or not source.strip():
        errors.append(f"{name}.source 必须是非空字符串")
    if not isinstance(version, str) or not version.strip():
        errors.append(f"{name}.version 必须是非空字符串")
