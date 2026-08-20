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
SCENARIO_ROOT = EDGE_SERVICE_ROOT.parent / "scenarios" / "bearing" / "edge"


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
    scenario_root = SCENARIO_ROOT
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


def test_edge_perception_shim_forwards_processor_to_scenario_authority() -> None:
    from scenarios.bearing.edge.processor import BearingEdgePerception as _Scenario

    import edge_perception.processor as shim_processor

    assert shim_processor.BearingEdgePerception is _Scenario
    assert shim_processor.EdgePerception is _Scenario


def test_edge_perception_shim_forwards_config_to_scenario_settings() -> None:
    from scenarios.bearing.edge.settings import (
        ConstantDetectionConfig as _Constant,
        PerceptionConfig as _ScenarioConfig,
        file_sha256 as _sha,
    )

    import edge_perception.config as shim_config

    assert shim_config.PerceptionConfig is _ScenarioConfig
    assert shim_config.ConstantDetectionConfig is _Constant
    assert shim_config.file_sha256 is _sha


def test_edge_perception_shim_forwards_contracts_to_core_authority() -> None:
    import core.edge_perception_contracts as core_contracts

    import edge_perception.contracts as shim_contracts

    assert shim_contracts.ModuleResult is core_contracts.ModuleResult
    assert shim_contracts.ModuleStatus is core_contracts.ModuleStatus
    assert shim_contracts.PerceptionInvocationContext is core_contracts.PerceptionInvocationContext
    assert shim_contracts.DOWNSAMPLING_FAILED == core_contracts.DOWNSAMPLING_FAILED
    assert shim_contracts.PERCEPTION_FAILED == core_contracts.PERCEPTION_FAILED


def test_fir_asset_lives_only_at_scenario_authority() -> None:
    shim_assets = EDGE_SERVICE_ROOT / "src" / "edge_perception" / "assets"
    assert not (shim_assets / "fir_64k_to_16k_369.txt").exists()

    scenario_asset = SCENARIO_ROOT / "assets" / "fir_64k_to_16k_369.txt"
    assert scenario_asset.is_file()


def test_fir_generator_writes_single_authoritative_asset(tmp_path: Path) -> None:
    script_path = EDGE_SERVICE_ROOT / "scripts" / "generate_development_fir.py"
    spec = importlib.util.spec_from_file_location("generate_development_fir", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "scenario" / "fir.txt"
    module.OUTPUT = target

    module.main()

    assert target.is_file()


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