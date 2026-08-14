# -*- coding: utf-8 -*-
"""离线生成开发测试用 64 kHz → 16 kHz、369 tap Kaiser FIR资产。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "src" / "edge_perception" / "assets" / "fir_64k_to_16k_369.txt"
SCENARIO_OUTPUT = (
    REPO.parent
    / "scenarios"
    / "bearing"
    / "edge"
    / "assets"
    / "fir_64k_to_16k_369.txt"
)


def main() -> None:
    indexes = np.arange(369, dtype=np.float64) - 184.0
    coefficients = (
        (2.0 * 7500.0 / 64000.0)
        * np.sinc((2.0 * 7500.0 / 64000.0) * indexes)
        * np.kaiser(369, 8.41)
    )
    coefficients /= np.sum(coefficients, dtype=np.float64)
    header = (
        "development_test asset; fs=64000; cutoff=7500; taps=369; "
        f"kaiser_beta=8.41; numpy={np.__version__}"
    )
    for output in (OUTPUT, SCENARIO_OUTPUT):
        output.parent.mkdir(parents=True, exist_ok=True)
        np.savetxt(output, coefficients, fmt="%.18e", header=header)
        print(f"{output}: {hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
