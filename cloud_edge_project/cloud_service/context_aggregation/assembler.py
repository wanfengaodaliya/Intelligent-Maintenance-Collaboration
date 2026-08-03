from __future__ import annotations

import numpy as np

from .contracts import HIGH_RATE_CHANNELS, LoadedPacket, RawWindow


def assemble(packets: list[LoadedPacket]) -> RawWindow:
    channels = {
        name: np.concatenate([np.asarray(item.packet["data"][name]["values"], dtype=np.float64) for item in packets])
        for name in HIGH_RATE_CHANNELS
    }
    return RawWindow(
        channels=channels,
        relative_positions=np.asarray([item.relative_position for item in packets], dtype=np.int64),
        packet_start_samples=np.arange(len(packets), dtype=np.int64) * 3_200,
    )
