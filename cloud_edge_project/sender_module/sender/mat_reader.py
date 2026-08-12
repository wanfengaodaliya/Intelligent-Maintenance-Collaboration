from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
from scipy.io import loadmat


class MatDataError(ValueError):
    pass


@dataclass(frozen=True)
class SignalSeries:
    times: np.ndarray
    values: np.ndarray
    sample_rate_hz: int

    def __post_init__(self) -> None:
        if self.times.ndim != 1 or self.values.ndim != 1:
            raise MatDataError("signal times and values must be one-dimensional")
        if len(self.times) != len(self.values):
            raise MatDataError("signal times and values have different lengths")
        if len(self.times) == 0:
            raise MatDataError("signal cannot be empty")
        if np.any(np.diff(self.times) < 0):
            raise MatDataError("signal time axis must be ordered")


@dataclass(frozen=True)
class SignalWindow:
    sequence_number: int
    start_seconds: float
    end_seconds: float
    start_index: int
    end_index: int
    window_index: int
    data: dict[str, object]


@dataclass(frozen=True)
class MatRecord:
    source_path: Path
    series: dict[str, SignalSeries]
    temperature_times: np.ndarray
    temperature_values: np.ndarray

    def __post_init__(self) -> None:
        if self.temperature_times.ndim != 1 or self.temperature_values.ndim != 1:
            raise MatDataError("temperature arrays must be one-dimensional")
        if len(self.temperature_times) != len(self.temperature_values) or not len(self.temperature_times):
            raise MatDataError("temperature times and values are invalid")

    def windows(self, *, duration_ms: int, count: int) -> Iterator[SignalWindow]:
        if duration_ms <= 0 or count <= 0:
            raise MatDataError("window duration and count must be positive")

        duration_seconds = duration_ms / 1000.0
        for zero_based_index in range(count):
            start = zero_based_index * duration_seconds
            end = start + duration_seconds
            data: dict[str, object] = {}
            vibration_start_index = 0
            vibration_end_index = 0

            for name, signal in self.series.items():
                samples_per_window = round(signal.sample_rate_hz * duration_seconds)
                origin = int(np.searchsorted(signal.times, 0.0, side="left"))
                first = origin + zero_based_index * samples_per_window
                last = min(first + samples_per_window, len(signal.values))
                values = signal.values[first:last].astype(float, copy=False).tolist()
                if name == "vibration":
                    vibration_start_index = first
                    vibration_end_index = last
                data[name] = {
                    "sample_rate_hz": signal.sample_rate_hz,
                    "sample_count": len(values),
                    "values": values,
                }

            temperature_index = int(
                np.searchsorted(self.temperature_times, end, side="right") - 1
            )
            if temperature_index < 0:
                temperature_index = 0
            data["bearing_module_temperature_c"] = float(
                self.temperature_values[temperature_index]
            )

            yield SignalWindow(
                sequence_number=zero_based_index + 1,
                start_seconds=start,
                end_seconds=end,
                start_index=vibration_start_index,
                end_index=vibration_end_index,
                window_index=zero_based_index,
                data=data,
            )


SOURCE_TO_PACKET = {
    "vibration_1": ("vibration", 64000),
    "phase_current_1": ("phase_current_1_A", 64000),
    "phase_current_2": ("phase_current_2_A", 64000),
    "speed": ("shaft_speed_rpm", 4000),
    "torque": ("load_torque_nm", 4000),
    "force": ("bearing_radial_load_n", 4000),
}
TEMPERATURE_SOURCE = "temp_2_bearing_module"


def _root_struct(mat: dict[str, object]) -> object:
    roots = [value for key, value in mat.items() if not key.startswith("__")]
    if len(roots) != 1 or not hasattr(roots[0], "X") or not hasattr(roots[0], "Y"):
        raise MatDataError("MAT file does not contain the expected Paderborn structure")
    return roots[0]


def load_mat_record(path: Path | str) -> MatRecord:
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise MatDataError(f"MAT file does not exist: {source_path}")

    try:
        raw = loadmat(source_path, squeeze_me=True, struct_as_record=False)
    except (OSError, ValueError, TypeError) as exc:
        raise MatDataError(f"cannot read MAT file: {exc}") from exc

    root = _root_struct(raw)
    x_axes = [
        np.asarray(item.Data, dtype=float).reshape(-1)
        for item in np.atleast_1d(root.X)
    ]

    series: dict[str, SignalSeries] = {}
    temperature_times: np.ndarray | None = None
    temperature_values: np.ndarray | None = None

    for item in np.atleast_1d(root.Y):
        source_name = str(item.Name)
        x_index = int(item.XIndex) - 1
        if not 0 <= x_index < len(x_axes):
            raise MatDataError(f"invalid XIndex for signal {source_name}")
        times = x_axes[x_index]
        values = np.asarray(item.Data, dtype=float).reshape(-1)

        if source_name == TEMPERATURE_SOURCE:
            temperature_times = times
            temperature_values = values
            continue
        if source_name in SOURCE_TO_PACKET:
            packet_name, sample_rate_hz = SOURCE_TO_PACKET[source_name]
            series[packet_name] = SignalSeries(times, values, sample_rate_hz)

    required_names = {name for name, _ in SOURCE_TO_PACKET.values()}
    if required_names - set(series):
        missing_names = sorted(required_names - set(series))
        raise MatDataError(f"missing required signals: {', '.join(missing_names)}")
    if temperature_times is None or temperature_values is None:
        raise MatDataError("missing bearing module temperature")

    return MatRecord(
        source_path=source_path,
        series=series,
        temperature_times=temperature_times,
        temperature_values=temperature_values,
    )
