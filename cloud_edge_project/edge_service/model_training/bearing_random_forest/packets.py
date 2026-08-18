from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cloud_edge_project.sender_module.sender.mat_reader import SignalWindow
from cloud_edge_project.sender_module.sender.packet import build_sensor_packet


_MAT_NAME = re.compile(
    r"^(N\d+_M\d+_F\d+)_([A-Z]+\d+)_(\d+)\.mat$",
    re.IGNORECASE,
)
_OFFLINE_EPOCH_NS = 1_700_000_000_000_000_000


class MatFilenameError(ValueError):
    pass


@dataclass(frozen=True)
class MatFilenameMetadata:
    operating_condition: str
    repeat_index: int


def parse_mat_filename(path: Path, bearing_id: str) -> MatFilenameMetadata:
    match = _MAT_NAME.fullmatch(path.name)
    if match is None or match.group(2).upper() != bearing_id.upper():
        raise MatFilenameError(f"MAT 文件名不符合 Paderborn 合同: {path.name}")
    return MatFilenameMetadata(match.group(1).upper(), int(match.group(3)))


def build_offline_packet(
    window: SignalWindow,
    *,
    bearing_ordinal: int,
    mat_ordinal: int,
) -> dict:
    data = dict(window.data)
    data["vibration"] = {**data["vibration"], "unit": "mm/s"}
    for channel in ("phase_current_1_A", "phase_current_2_A"):
        data[channel] = {**data[channel], "unit": "A"}
    return build_sensor_packet(
        device_id="device_01",
        task_id=f"sd_01_tk_{mat_ordinal:04d}",
        bearing_id=f"bearing_{bearing_ordinal:02d}",
        sender_id="sender_01",
        sequence_number=window.sequence_number,
        data=data,
        end_generate_timestamp_ns=(
            _OFFLINE_EPOCH_NS + mat_ordinal * 1_000 + window.sequence_number
        ),
    )
