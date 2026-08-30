"""Legacy import path for the bearing H5 activation probe."""

from compatibility.bearing_v12.edge_h5_runtime_exports import (
    H5_FEATURE_PIPELINE_VERSION,
    H5ProbeError,
    default_probe_dir,
    load_h5_probe,
    load_h5_probe_task,
    read_probe_manifest,
)

__all__ = [
    "H5_FEATURE_PIPELINE_VERSION",
    "H5ProbeError",
    "default_probe_dir",
    "load_h5_probe",
    "load_h5_probe_task",
    "read_probe_manifest",
]
