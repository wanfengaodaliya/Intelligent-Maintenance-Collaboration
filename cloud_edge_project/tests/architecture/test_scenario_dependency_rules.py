from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_CORE_SHIMS = {
    "bearing_actions.py",
    "bearing_workflow_contracts.py",
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


def test_core_does_not_import_bearing_plugin() -> None:
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "core").glob("*.py")
        if path.name not in LEGACY_CORE_SHIMS and _imports_bearing_plugin(path)
    ]

    assert offenders == []


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

    assert "build_cloud_scenario_registry" in imported_names
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
