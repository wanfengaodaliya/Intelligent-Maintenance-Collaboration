from __future__ import annotations

import ast
from pathlib import Path

from bootstrap.scenarios import build_scenario_registry
from scenarios.virtual_power_plant import VirtualPowerPlantPlugin


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VPP_ROOT = PROJECT_ROOT / "scenarios" / "virtual_power_plant"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_vpp_plugin_has_no_bearing_dependency() -> None:
    offenders = []
    for path in VPP_ROOT.glob("*.py"):
        imports = _imports(path)
        if any(
            name.startswith("scenarios.bearing")
            or name.startswith("compatibility.bearing_v12")
            for name in imports
        ):
            offenders.append(path.name)

    assert offenders == []


def test_default_registry_remains_bearing_only() -> None:
    assert build_scenario_registry().scenario_ids() == ("bearing",)
    assert build_scenario_registry(
        plugins=(VirtualPowerPlantPlugin(),)
    ).scenario_ids() == ("bearing", "virtual_power_plant")


def test_vpp_vocabulary_does_not_leak_into_platform_or_bearing_code() -> None:
    roots = (
        PROJECT_ROOT / "core",
        PROJECT_ROOT / "bootstrap",
        PROJECT_ROOT / "scheduler",
        PROJECT_ROOT / "edge_service",
        PROJECT_ROOT / "cloud_service",
        PROJECT_ROOT / "scenarios" / "bearing",
    )
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in roots
        for path in root.rglob("*.py")
        if "virtual_power_plant" in path.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []
