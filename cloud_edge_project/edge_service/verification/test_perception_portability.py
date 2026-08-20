from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from edge_perception import ModuleResult, PerceptionRegistry
from scenarios.bearing.edge import build_bearing_perception_config


EDGE_SERVICE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PERCEPTION_ROOT = EDGE_SERVICE_ROOT / "src" / "edge_perception"


class _Handler:
    def downsample(self, raw_packet, context):
        return ModuleResult.succeeded(raw_packet)

    def perceive(self, packet, context):
        return ModuleResult.succeeded(packet)


def test_perception_registry_resolves_scenario_without_runtime_coupling() -> None:
    registry = PerceptionRegistry()
    handler = _Handler()
    registry.register("Bearing", lambda: handler)

    assert registry.create(" bearing ") is handler


def test_bearing_config_keeps_current_defaults_and_supports_overrides() -> None:
    config = build_bearing_perception_config(
        {
            "EDGE_PERCEPTION_RUNNING_SPEED_THRESHOLD_RPM": "250",
            "EDGE_PERCEPTION_CONSTANT_THRESHOLD": "0.000001",
        }
    )

    assert config.running_speed_threshold_rpm == 250.0
    assert config.constant_detection["vibration"].threshold == 0.000001
    assert config.feature_zero_power_threshold == 1e-20
    assert config.validate() == []


def test_bearing_scenario_has_no_edge_service_imports() -> None:
    scenario_root = EDGE_SERVICE_ROOT.parent / "scenarios" / "bearing" / "edge"
    for filename in ("config.py", "handler.py", "processor.py", "settings.py"):
        source = (scenario_root / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        )
        assert not any(
            module == "edge_perception"
            or module.startswith("edge_perception.")
            or module == "edge_service"
            or module.startswith("edge_service.")
            for module in imports
        )


def test_scenario_copy_preserves_existing_perception_logic() -> None:
    scenario_root = EDGE_SERVICE_ROOT.parent / "scenarios" / "bearing" / "edge"
    legacy_config = (LEGACY_PERCEPTION_ROOT / "config.py").read_text(encoding="utf-8")
    scenario_config = (scenario_root / "settings.py").read_text(encoding="utf-8")
    assert scenario_config == legacy_config

    legacy_processor = (LEGACY_PERCEPTION_ROOT / "processor.py").read_text(
        encoding="utf-8"
    )
    expected_scenario_processor = (
        legacy_processor.replace(
            "from .config import PerceptionConfig, file_sha256",
            "from .settings import PerceptionConfig, file_sha256",
        )
        .replace(
            "from .contracts import (",
            "from core.edge_perception_contracts import (",
        )
        .replace("class EdgePerception:", "class BearingEdgePerception:")
    )
    scenario_processor = (scenario_root / "processor.py").read_text(
        encoding="utf-8"
    )
    assert scenario_processor == expected_scenario_processor

    legacy_fir = np.loadtxt(
        LEGACY_PERCEPTION_ROOT / "assets" / "fir_64k_to_16k_369.txt"
    )
    scenario_fir = np.loadtxt(
        scenario_root / "assets" / "fir_64k_to_16k_369.txt"
    )
    assert np.array_equal(scenario_fir, legacy_fir)


def test_fir_generator_updates_legacy_and_scenario_assets(tmp_path: Path) -> None:
    script_path = EDGE_SERVICE_ROOT / "scripts" / "generate_development_fir.py"
    spec = importlib.util.spec_from_file_location("generate_development_fir", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUTPUT = tmp_path / "legacy" / "fir.txt"
    module.SCENARIO_OUTPUT = tmp_path / "scenario" / "fir.txt"

    module.main()

    assert module.OUTPUT.read_bytes() == module.SCENARIO_OUTPUT.read_bytes()


def test_edge_launcher_imports_application_outside_project_directory(
    tmp_path: Path,
) -> None:
    # AUD-12: the launcher imports the H5 diagnostic model which needs torch.
    # Skip only in torch-less environments; the Conda "moment" env runs it fully.
    pytest.importorskip(
        "torch",
        reason="edge app import requires the torch runtime (Conda moment env)",
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["EDGE_STATUS_REPORTER_ENABLED"] = "false"
    completed = subprocess.run(
        [
            sys.executable,
            str(EDGE_SERVICE_ROOT / "run_edge_service.py"),
            "--check-import",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
