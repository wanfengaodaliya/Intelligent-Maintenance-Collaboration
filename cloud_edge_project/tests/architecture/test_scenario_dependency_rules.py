from __future__ import annotations

import ast
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_CORE_SHIMS = {
    "bearing_actions.py",
    "bearing_workflow_contracts.py",
}
BEARING_INGESTION_MODULES = {
    "mat_reader.py": {
        "MatDataError",
        "SignalSeries",
        "SignalWindow",
        "MatRecord",
        "load_mat_record",
    },
    "packet.py": {
        "PacketValidationError",
        "build_sensor_packet",
        "serialize_packet",
    },
    "source_mapping.py": {
        "extract_paderborn_bearing_code",
        "PacketSourceMappingStore",
    },
}
EDGE_H5_MODULES = {
    "h5_features.py": ("features.py", {"_compute_single", "normalize_features"}),
    "h5_network.py": ("network.py", {"PhysicalFusionModel"}),
    "distilled_h5_model.py": (
        "distilled_h5_model.py",
        {"H5ModelArtifactError", "DistilledH5DiagnosticModel"},
    ),
}


def _imports_bearing_plugin(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("scenarios.bearing") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("scenarios.bearing"):
                return True
    return False


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_module_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def test_core_does_not_import_bearing_plugin() -> None:
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "core").glob("*.py")
        if path.name not in LEGACY_CORE_SHIMS and _imports_bearing_plugin(path)
    ]

    assert offenders == []


def test_generic_decision_engines_do_not_contain_bearing_vocabulary() -> None:
    forbidden_roots = {"cloud_service", "compatibility", "edge_service", "scenarios"}
    for filename in ("consistency_engine.py", "arbitration_engine.py"):
        source = (PROJECT_ROOT / "core" / filename).read_text(encoding="utf-8")
        assert "bearing" not in source.lower()
        tree = ast.parse(source, filename=filename)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        assert imported_roots.isdisjoint(forbidden_roots)


def test_model_lifecycle_core_is_scenario_neutral() -> None:
    path = PROJECT_ROOT / "core" / "model_lifecycle.py"
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()
    assert all(word not in lowered for word in ("bearing", "moment", "h5"))
    assert not _imports_bearing_plugin(path)


def test_common_schemas_uses_compatibility_boundary() -> None:
    path = PROJECT_ROOT / "common" / "schemas.py"

    assert not _imports_bearing_plugin(path)


def test_bootstrap_scenario_assembly_imports_bearing_plugin() -> None:
    bootstrap_files = list((PROJECT_ROOT / "bootstrap").glob("*.py"))
    importers = [path.name for path in bootstrap_files if _imports_bearing_plugin(path)]

    assert importers == ["scenarios.py"]


def test_edge_app_uses_registry_instead_of_h5_implementation_imports() -> None:
    app_path = PROJECT_ROOT / "edge_service" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "build_edge_scenario_registry" in imported_names
    assert not {
        "H5_RUNTIME_MODEL_VERSION",
        "LocalH5ClientConfig",
        "LocalH5ModelClient",
        "initialize_model_store",
    }.intersection(imported_names)
    assert not _imports_bearing_plugin(app_path)


def test_cloud_app_uses_registry_instead_of_bearing_implementation_imports() -> None:
    app_path = PROJECT_ROOT / "cloud_service" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert {"build_cloud_scenario_registry", "STORAGE_PROVIDER"}.issubset(
        imported_names
    )
    assert not _imports_bearing_plugin(app_path)


def test_sender_controller_uses_registry_instead_of_bearing_input_modules() -> None:
    controller_path = PROJECT_ROOT / "sender_module" / "sender" / "controller.py"
    tree = ast.parse(
        controller_path.read_text(encoding="utf-8"),
        filename=str(controller_path),
    )
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "build_sender_scenario_registry" in imported_names
    assert not {
        "load_mat_record",
        "build_sensor_packet",
        "PacketSourceMappingStore",
    }.intersection(imported_names)
    assert not _imports_bearing_plugin(controller_path)


@pytest.mark.parametrize("filename", BEARING_INGESTION_MODULES)
def test_legacy_bearing_ingestion_modules_use_compatibility_boundary(
    filename: str,
) -> None:
    sender_root = PROJECT_ROOT / "sender_module" / "sender"
    imported_modules = _imported_modules(sender_root / filename)

    assert "compatibility.bearing_v12.ingestion_exports" in imported_modules
    assert not any(
        module.startswith("scenarios.bearing") for module in imported_modules
    )


@pytest.mark.parametrize("filename", BEARING_INGESTION_MODULES)
def test_bearing_input_provider_uses_scenario_local_ingestion_modules(
    filename: str,
) -> None:
    provider_path = (
        PROJECT_ROOT / "scenarios" / "bearing" / "ingestion" / "provider.py"
    )
    imported_modules = _imported_modules(provider_path)
    module_name = Path(filename).stem

    assert f"scenarios.bearing.ingestion.{module_name}" in imported_modules
    assert f"sender.{module_name}" not in imported_modules


@pytest.mark.parametrize(
    ("filename", "business_names"), BEARING_INGESTION_MODULES.items()
)
def test_bearing_ingestion_business_definitions_have_one_owner(
    filename: str,
    business_names: set[str],
) -> None:
    scenario_root = PROJECT_ROOT / "scenarios" / "bearing" / "ingestion"
    sender_root = PROJECT_ROOT / "sender_module" / "sender"
    scenario_path = scenario_root / filename

    assert scenario_path.is_file()
    assert business_names.issubset(_defined_names(scenario_path))
    assert _defined_names(sender_root / filename).isdisjoint(business_names)


def test_legacy_bearing_ingestion_modules_are_thin_explicit_shims() -> None:
    sender_root = PROJECT_ROOT / "sender_module" / "sender"
    for filename in BEARING_INGESTION_MODULES:
        path = sender_root / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]

        assert assignments == ["__all__"]
        assert tree.body and _is_module_docstring(tree.body[0])
        assert all(
            isinstance(node, (ast.ImportFrom, ast.Assign))
            for node in tree.body[1:]
        )
        assert all(
            alias.name != "*"
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )


def test_bearing_ingestion_compatibility_exports_are_explicit() -> None:
    path = (
        PROJECT_ROOT
        / "compatibility"
        / "bearing_v12"
        / "ingestion_exports.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert all(
        alias.name != "*"
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )


@pytest.mark.parametrize("legacy_filename", EDGE_H5_MODULES)
def test_legacy_edge_h5_modules_use_compatibility_boundary(
    legacy_filename: str,
) -> None:
    legacy_path = (
        PROJECT_ROOT / "edge_service" / "src" / "edge_diagnosis" / legacy_filename
    )
    imported_modules = _imported_modules(legacy_path)

    assert "compatibility.bearing_v12.edge_h5_exports" in imported_modules
    assert not any(
        module.startswith("scenarios.bearing") for module in imported_modules
    )


@pytest.mark.parametrize(
    ("legacy_filename", "scenario_filename", "business_names"),
    tuple(
        (legacy_filename, scenario_filename, business_names)
        for legacy_filename, (scenario_filename, business_names) in EDGE_H5_MODULES.items()
    ),
)
def test_bearing_edge_h5_business_definitions_have_one_owner(
    legacy_filename: str,
    scenario_filename: str,
    business_names: set[str],
) -> None:
    scenario_path = (
        PROJECT_ROOT
        / "scenarios"
        / "bearing"
        / "edge_inference"
        / "h5"
        / scenario_filename
    )
    legacy_path = (
        PROJECT_ROOT / "edge_service" / "src" / "edge_diagnosis" / legacy_filename
    )

    assert scenario_path.is_file()
    assert business_names.issubset(_defined_names(scenario_path))
    assert _defined_names(legacy_path).isdisjoint(business_names)
    assert not any(
        module.startswith("edge_diagnosis.h5")
        for module in _imported_modules(scenario_path)
    )


def test_legacy_edge_h5_modules_are_thin_explicit_shims() -> None:
    legacy_root = PROJECT_ROOT / "edge_service" / "src" / "edge_diagnosis"
    for legacy_filename in EDGE_H5_MODULES:
        path = legacy_root / legacy_filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        ]

        assert assignments == ["__all__"]
        assert tree.body and _is_module_docstring(tree.body[0])
        assert all(
            isinstance(node, (ast.ImportFrom, ast.Assign))
            for node in tree.body[1:]
        )
        assert all(
            alias.name != "*"
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        )


def test_edge_h5_compatibility_exports_are_explicit() -> None:
    path = PROJECT_ROOT / "compatibility" / "bearing_v12" / "edge_h5_exports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    assert assignments == ["__all__"]
    assert tree.body and _is_module_docstring(tree.body[0])
    assert all(
        isinstance(node, (ast.ImportFrom, ast.Assign))
        for node in tree.body[1:]
    )
    assert all(
        alias.name != "*"
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )


def test_bearing_edge_h5_business_definitions_have_exactly_one_owner() -> None:
    scenario_root = (
        PROJECT_ROOT / "scenarios" / "bearing" / "edge_inference" / "h5"
    )
    compatibility_path = (
        PROJECT_ROOT / "compatibility" / "bearing_v12" / "edge_h5_exports.py"
    )
    legacy_root = PROJECT_ROOT / "edge_service" / "src" / "edge_diagnosis"
    candidate_paths = [
        *scenario_root.glob("*.py"),
        compatibility_path,
        *(legacy_root / filename for filename in EDGE_H5_MODULES),
    ]

    for scenario_filename, business_names in EDGE_H5_MODULES.values():
        expected_owner = scenario_root / scenario_filename
        for business_name in business_names:
            owners = [
                path for path in candidate_paths if business_name in _defined_names(path)
            ]
            assert owners == [expected_owner]


def test_edge_image_copies_compatibility_boundary() -> None:
    dockerfile = (PROJECT_ROOT / "edge_service" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY compatibility ./compatibility" in dockerfile.splitlines()
