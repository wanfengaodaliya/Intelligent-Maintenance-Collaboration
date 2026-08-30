from __future__ import annotations

import ast
from pathlib import Path

from bootstrap.scenarios import (
    build_cloud_scenario_registry,
    build_edge_scenario_registry,
    build_scenario_registry,
    build_sender_scenario_registry,
)
from core.scenario_plugin import CLOUD_DIAGNOSIS, EDGE_INFERENCE, INPUT_ADAPTER
from tests.fixtures.scenarios.reference_inspection import ReferenceInspectionPlugin


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "scenarios" / "reference_inspection"
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
    production_files = list(PROJECT_ROOT.glob("*.py"))
    production_files.extend(
        path
        for directory in PROJECT_ROOT.iterdir()
        if directory.is_dir()
        and directory.name != "tests"
        and not directory.name.startswith(".")
        and directory.name != "__pycache__"
        for path in directory.rglob("*.py")
    )
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in production_files
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


def test_reference_fixture_runs_through_production_service_assembly() -> None:
    builders_and_capabilities = (
        (build_sender_scenario_registry, INPUT_ADAPTER),
        (build_edge_scenario_registry, EDGE_INFERENCE),
        (build_cloud_scenario_registry, CLOUD_DIAGNOSIS),
    )

    for builder, capability in builders_and_capabilities:
        registry = builder(plugins=(ReferenceInspectionPlugin(),))

        assert registry.scenario_ids() == ("bearing", "reference_inspection")
        assert (
            registry.require_provider("reference_inspection", capability) is not None
        )


def test_reference_fixture_contains_no_bearing_domain_vocabulary() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in FIXTURE_ROOT.glob("*.py")
    )

    assert "bearing" not in source
