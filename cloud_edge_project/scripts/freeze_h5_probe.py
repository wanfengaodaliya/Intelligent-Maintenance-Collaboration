"""Freeze the canonical distilled-H5 runtime probe from a Paderborn MAT file."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sender_module.sender.mat_reader import load_mat_record  # noqa: E402


SOURCE_FILE_NAME = "N09_M07_F10_K001_1.mat"
SOURCE_SHA256 = "c55f4c6a5d6d9a83d18d24dce4f164ffc85721e4e358df1c1ac9886583c2269f"
WINDOW_INDEX = 20
DURATION_MS = 50
PROBE_CONTRACT_VERSION = "v1"
FEATURE_PIPELINE_VERSION = "edge_feature_v1"
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT
    / "edge_service"
    / "resources"
    / "model_probes"
    / "distilled_h5"
    / PROBE_CONTRACT_VERSION
)

PROBE_CHANNELS = {
    "vibration": (64_000, 3_200),
    "shaft_speed_rpm": (4_000, 200),
    "load_torque_nm": (4_000, 200),
    "bearing_radial_load_n": (4_000, 200),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_arrays(source: Path) -> tuple[dict[str, np.ndarray], object]:
    record = load_mat_record(source)
    window = next(
        itertools.islice(
            record.windows(duration_ms=DURATION_MS, count=WINDOW_INDEX + 1),
            WINDOW_INDEX,
            WINDOW_INDEX + 1,
        ),
        None,
    )
    if window is None or window.window_index != WINDOW_INDEX:
        raise ValueError(f"MAT does not contain window_index={WINDOW_INDEX}")

    arrays: dict[str, np.ndarray] = {}
    for name, (sample_rate_hz, sample_count) in PROBE_CHANNELS.items():
        channel = window.data.get(name)
        if not isinstance(channel, dict):
            raise ValueError(f"probe channel is missing: {name}")
        if channel.get("sample_rate_hz") != sample_rate_hz:
            raise ValueError(f"probe channel sample rate mismatch: {name}")
        values = np.asarray(channel.get("values"), dtype=np.float32)
        if values.shape != (sample_count,) or not np.isfinite(values).all():
            raise ValueError(f"probe channel data is invalid: {name}")
        arrays[name] = values

    temperature = np.asarray(
        window.data.get("bearing_module_temperature_c"), dtype=np.float32
    )
    if temperature.shape != () or not np.isfinite(temperature):
        raise ValueError("probe temperature is invalid")
    arrays["bearing_module_temperature_c"] = temperature
    return arrays, window


def freeze_probe(source: Path, output_dir: Path) -> tuple[Path, Path]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source MAT does not exist: {source}")
    if source.name != SOURCE_FILE_NAME:
        raise ValueError(f"expected source file {SOURCE_FILE_NAME}, got {source.name}")
    source_sha256 = _sha256(source)
    if source_sha256 != SOURCE_SHA256:
        raise ValueError(
            "source MAT SHA256 mismatch: "
            f"expected={SOURCE_SHA256} actual={source_sha256}"
        )

    arrays, window = _probe_arrays(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    probe_path = output_dir / "probe.npz"
    probe_tmp = output_dir / "probe.tmp.npz"
    np.savez_compressed(probe_tmp, **arrays)
    probe_tmp.replace(probe_path)

    channel_manifest = {
        name: {
            "dtype": str(values.dtype),
            "shape": list(values.shape),
            **(
                {"sample_rate_hz": PROBE_CHANNELS[name][0]}
                if name in PROBE_CHANNELS
                else {}
            ),
        }
        for name, values in arrays.items()
    }
    manifest = {
        "schema_version": 1,
        "probe_id": "distilled_h5_real_probe_v1",
        "probe_contract_version": PROBE_CONTRACT_VERSION,
        "feature_pipeline_version": FEATURE_PIPELINE_VERSION,
        "model_type": "distilled_h5",
        "coverage": {
            "model_run": True,
            "build_evidence": False,
            "phase_current_channels_included": False,
        },
        "source": {
            "file_name": source.name,
            "sha256": source_sha256,
            "size_bytes": source.stat().st_size,
            "extractor": "sender_module.sender.mat_reader.MatRecord.windows",
            "window_index_zero_based": WINDOW_INDEX,
            "duration_ms": DURATION_MS,
            "start_seconds": window.start_seconds,
            "end_seconds": window.end_seconds,
        },
        "artifact": {
            "file_name": probe_path.name,
            "sha256": _sha256(probe_path),
            "channels": channel_manifest,
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_tmp = output_dir / "manifest.tmp"
    manifest_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_tmp.replace(manifest_path)
    return probe_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze the canonical real-data probe for distilled H5 run()."
    )
    parser.add_argument("--source", required=True, type=Path, help="Source MAT path")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()
    probe_path, manifest_path = freeze_probe(args.source, args.output_dir)
    print(f"probe={probe_path}")
    print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
