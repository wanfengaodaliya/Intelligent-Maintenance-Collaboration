from __future__ import annotations

import ast
from pathlib import Path

from bootstrap.scenarios import (
    build_cloud_scenario_registry,
    build_edge_scenario_registry,
    build_scenario_registry,
    build_sender_scenario_registry,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "scenarios" / "reference_inspection"
)
PRODUCTION_ROOTS = (
    "bootstrap",
    "core",
    "scheduler",
    "edge_service",
    "cloud_service",
)


def _import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_reference_fixture_does_not_import_bearing_code() -> None:
    offenders = []
    for path in FIXTURE_ROOT.glob("*.py"):
        imports = _import_names(path)
        if any(
            name.startswith("scenarios.bearing")
            or name.startswith("compatibility.bearing_v12")
            for name in imports
        ):
            offenders.append(path.name)

    assert offenders == []


def test_reference_vocabulary_does_not_leak_into_production_modules() -> None:
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in PRODUCTION_ROOTS
        for path in (PROJECT_ROOT / root).rglob("*.py")
        if "reference_inspection" in path.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []


def test_production_registry_builders_do_not_load_reference_fixture() -> None:
    registries = (
        build_scenario_registry(),
        build_edge_scenario_registry(),
        build_cloud_scenario_registry(),
        build_sender_scenario_registry(),
    )

    assert all(registry.scenario_ids() == ("bearing",) for registry in registries)


def test_reference_fixture_contains_no_bearing_domain_vocabulary() -> None:
    forbidden = (
        "bearing_id",
        "bearing_results",
        "expected_bearing_count",
        "outer_ring_damage",
        "inner_ring_damage",
        "radial_load",
        "bearing_temperature",
        "bearing_edge_inference",
    )
    source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in FIXTURE_ROOT.glob("*.py")
    )

    assert all(word not in source for word in forbidden)
