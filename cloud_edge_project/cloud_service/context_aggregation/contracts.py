from __future__ import annotations

from dataclasses import dataclass

import numpy as np


HIGH_RATE_CHANNELS = ("vibration", "phase_current_1_A", "phase_current_2_A")


class AggregationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class LoadedPacket:
    packet: dict
    index: dict
    relative_position: int


@dataclass(frozen=True)
class RawWindow:
    channels: dict[str, np.ndarray]
    relative_positions: np.ndarray
    packet_start_samples: np.ndarray
