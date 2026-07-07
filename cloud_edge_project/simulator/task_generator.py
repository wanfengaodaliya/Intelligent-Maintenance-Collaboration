"""Generate first-stage bearing timeseries packets."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from random import Random
from typing import Any

from common.config import PROJECT_ROOT
from common.schemas import DATA_TYPE, DURATION_MS, SAMPLE_COUNT, SAMPLE_RATE_HZ, validate_sensor_packet


BASE_START_NS = 1781920800000000000


def _waveform(kind: str, seed: int) -> list[float]:
    rng = Random(seed)
    values: list[float] = []
    for index in range(SAMPLE_COUNT):
        angle = 2 * math.pi * index / 40
        baseline = 0.018 * math.sin(angle)
        noise = rng.uniform(-0.004, 0.004)
        if kind == "abnormal":
            impact = 0.08 if index % 97 == 0 else 0.0
            value = baseline + 0.035 * math.sin(5 * angle) + impact + noise
        else:
            value = baseline + noise
        values.append(round(value, 6))
    return values


def generate_sensor_packet(sequence_number: int = 1, kind: str = "abnormal") -> dict[str, Any]:
    if kind not in {"normal", "abnormal"}:
        raise ValueError("kind must be normal or abnormal")
    start_ns = BASE_START_NS + (sequence_number - 1) * DURATION_MS * 1_000_000
    end_ns = start_ns + DURATION_MS * 1_000_000
    if kind == "abnormal":
        current = 1.82
        temperature = 68.5
        load = 0.86
    else:
        current = 1.24
        temperature = 43.2
        load = 0.54
    packet = {
        "packet_id": f"batch_{sequence_number:06d}",
        "device_id": "K001",
        "sensor_id": "sensor_K001",
        "sequence_number": sequence_number,
        "start_timestamp_ns": start_ns,
        "end_timestamp_ns": end_ns,
        "duration_ms": DURATION_MS,
        "data": {
            "data_type": DATA_TYPE,
            "vibration_sample_rate_hz": SAMPLE_RATE_HZ,
            "vibration_sample_count": SAMPLE_COUNT,
            "vibration": _waveform(kind, sequence_number),
            "current": current,
            "temperature": temperature,
            "speed": 899.7,
            "load": load,
        },
    }
    return validate_sensor_packet(packet)


def write_examples(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    examples = {
        "sensor_packet_normal.json": generate_sensor_packet(1, "normal"),
        "sensor_packet_abnormal.json": generate_sensor_packet(2, "abnormal"),
    }
    for name, packet in examples.items():
        (output_dir / name).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate API-compatible SensorPacket examples.")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "examples"), help="output directory")
    args = parser.parse_args()
    write_examples(Path(args.out))
    print(f"wrote examples to {args.out}")


if __name__ == "__main__":
    main()

