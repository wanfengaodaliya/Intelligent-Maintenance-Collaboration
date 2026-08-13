from __future__ import annotations

import math
from numbers import Real
from typing import Any


FEATURE_COLUMNS = (
    "vibration.rms",
    "vibration.absolute_peak",
    "vibration.kurtosis",
    "vibration.dominant_frequency_hz",
    "vibration.band_power_ratio_500_2000",
    "vibration.spectral_entropy",
    "phase_current_1.rms_a",
    "phase_current_1.absolute_peak_a",
    "phase_current_2.rms_a",
    "phase_current_2.absolute_peak_a",
    "current_relationship.current_imbalance_ratio",
    "operating_context.shaft_speed_rpm.mean",
    "operating_context.shaft_speed_rpm.last",
    "operating_context.shaft_speed_rpm.minimum",
    "operating_context.shaft_speed_rpm.maximum",
    "operating_context.shaft_speed_rpm.standard_deviation",
    "operating_context.load_torque_nm.mean",
    "operating_context.load_torque_nm.last",
    "operating_context.load_torque_nm.minimum",
    "operating_context.load_torque_nm.maximum",
    "operating_context.load_torque_nm.standard_deviation",
    "operating_context.bearing_radial_load_n.mean",
    "operating_context.bearing_radial_load_n.last",
    "operating_context.bearing_radial_load_n.minimum",
    "operating_context.bearing_radial_load_n.maximum",
    "operating_context.bearing_radial_load_n.standard_deviation",
    "operating_context.bearing_module_temperature_c",
)


class FeatureValueError(ValueError):
    pass


def flatten_perception_features(perception_result: dict[str, Any]) -> dict[str, float]:
    root: Any = perception_result.get("features")
    output: dict[str, float] = {}
    for path in FEATURE_COLUMNS:
        value = root
        try:
            for part in path.split("."):
                value = value[part]
        except (KeyError, TypeError) as exc:
            raise FeatureValueError(f"缺少特征: {path}") from exc
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise FeatureValueError(f"特征必须是有限数值: {path}")
        output[path] = float(value)
    return output
