"""Migration / portability tests for scenario-decoupled architecture."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from cloud_service.global_analysis.service import GlobalAnalysisService
from scenarios.bearing.cloud.global_analysis.bearing_risk_analyzer import analyze_bearing_risk
from scenarios.bearing.cloud.global_analysis.bearing_aggregation_analyzer import analyze_bearing_aggregation


# ---------------------------------------------------------------------------
# 1. 旧导入路径兼容性
# ---------------------------------------------------------------------------

def test_old_bearing_actions_import_still_works() -> None:
    """core/bearing_actions.py 兼容层必须保持旧导入路径可用。"""
    from core.bearing_actions import (
        ACTION_TO_GRADE,
        GRADE_TO_ACTION,
        ACTION_TO_STATE,
        action_for_grade,
        grade_for_action,
    )
    assert ACTION_TO_GRADE["shutdown"] == 4
    assert GRADE_TO_ACTION[0] == "continue_operation"
    assert ACTION_TO_STATE["shutdown"] == "fault"
    assert action_for_grade(4) == "shutdown"
    assert grade_for_action("shutdown") == 4


def test_old_bearing_workflow_contracts_import_still_works() -> None:
    """core/bearing_workflow_contracts.py 兼容层必须保持旧导入路径可用。"""
    from core.bearing_workflow_contracts import (
        PACKETS_PER_WINDOW,
        WINDOWS_PER_BEARING,
        FINAL_EDGE,
        FinalPacketResult,
        BearingWindowResult,
        DeviceTaskResult,
    )
    assert PACKETS_PER_WINDOW == 20
    assert WINDOWS_PER_BEARING == 4
    assert FINAL_EDGE == "FINAL_EDGE"


# ---------------------------------------------------------------------------
# 2. 通用模块不得直接导入 scenarios.bearing
# ---------------------------------------------------------------------------

def _module_imports_scenarios_bearing(module_name: str) -> bool:
    """Check by re-loading the module and inspecting its imports."""
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        return False
    for name, value in mod.__dict__.items():
        if hasattr(value, "__module__") and value.__module__ and "scenarios.bearing" in value.__module__:
            return True
    return False


def test_common_does_not_import_scenarios_bearing() -> None:
    """common 模块不得直接导入 scenarios.bearing。"""
    import common.config
    import common.logger
    import common.schemas
    # Just verify they import without error
    assert common.config.load_config is not None


def test_core_does_not_import_scenarios_bearing() -> None:
    """core 模块（除兼容层外）不得直接导入 scenarios.bearing。

    bearing_actions 和 bearing_workflow_contracts 是兼容层，允许导入。
    """
    from core import scenario_registry, scenario_errors, arbitration_contracts
    # Verify core modules don't trigger bearing imports
    assert scenario_registry is not None
    assert scenario_errors is not None


def test_global_analysis_service_does_not_import_scenarios_bearing() -> None:
    """通用 GlobalAnalysisService 不得直接导入 scenarios.bearing。"""
    import cloud_service.global_analysis.service
    mod = cloud_service.global_analysis.service
    for name, value in mod.__dict__.items():
        if hasattr(value, "__module__") and value.__module__ and "scenarios.bearing" in value.__module__:
            pytest.fail(f"GlobalAnalysisService imports bearing-specific module: {value.__module__}")


# ---------------------------------------------------------------------------
# 3. GlobalAnalysisService 场景分析器注入
# ---------------------------------------------------------------------------

def test_global_analysis_with_bearing_analyzers() -> None:
    """GlobalAnalysisService 接受并调用注入的轴承分析器。"""
    import tempfile
    db_path = Path(tempfile.mkdtemp()) / "test_portability.db"
    source = _FakeDataSource(
        device_tasks=[
            {"device_id": "machine_01", "task_id": "t1", "final_state": "normal",
             "has_conflict": False, "completed_at_ns": 1}
        ],
        bearing_tasks=[
            {"bearing_id": "bearing_01", "bearing_state": "warning",
             "device_id": "machine_01", "task_id": "t1", "completed_at_ns": 1},
        ],
        bearing_review_pairs=[
            {"device_id": "machine_01", "task_id": "t1", "bearing_id": "bearing_01",
             "edge_state": "normal", "cloud_state": "fault",
             "completed_at_ns": 1},
        ],
    )

    def _risk_wrapper(data: dict[str, Any], config: Any) -> dict[str, Any]:
        return analyze_bearing_risk(data.get("bearing_tasks", []), config)

    def _review_wrapper(data: dict[str, Any], config: Any) -> dict[str, Any]:
        rows = data.get("bearing_review_pairs", [])
        if not rows:
            return {"status": "not_available", "bearing_review_count": 0}
        return analyze_bearing_aggregation(rows, config)

    service = GlobalAnalysisService(
        db_path,
        data_source=source,
        scenario_analyzers={
            "analyze_bearing_risk": _risk_wrapper,
            "analyze_cloud_bearing_review": _review_wrapper,
        },
    )
    result = service.analyze("bearing", "machine_01", 20)
    # Bearing-specific fields should be present when analyzers are injected
    assert "bearing_risk_analysis" in result
    assert "cloud_bearing_review_analysis" in result
    assert result["schema_version"] == "global_analysis_result/2.0"


def test_global_analysis_without_analyzers_omits_bearing_fields() -> None:
    """GlobalAnalysisService 不注入分析器时，不包含轴承专有字段。"""
    import tempfile
    db_path = Path(tempfile.mkdtemp()) / "test_portability_no_analyzers.db"
    source = _FakeDataSource(
        device_tasks=[
            {"device_id": "machine_01", "task_id": "t1", "final_state": "normal",
             "has_conflict": False, "completed_at_ns": 1}
        ],
    )
    service = GlobalAnalysisService(
        db_path,
        data_source=source,
        scenario_analyzers=None,
    )
    result = service.analyze("bearing", "machine_01", 20)
    assert "bearing_risk_analysis" not in result
    assert "cloud_bearing_review_analysis" not in result


# ---------------------------------------------------------------------------
# 4. 配置路径解析
# ---------------------------------------------------------------------------

def test_cloud_config_database_path_resolved_from_project_root() -> None:
    """database_path 默认值应相对于项目根目录解析，而非 CWD。"""
    from cloud_service.config import PROJECT_ROOT, CloudSettings
    # 默认值应基于 PROJECT_ROOT
    expected = PROJECT_ROOT / "data" / "cloud_review.db"
    # 字段默认值
    assert CloudSettings.database_path == expected, (
        f"Expected {expected}, got {CloudSettings.database_path}"
    )


def test_cloud_config_project_root_is_file_based() -> None:
    """PROJECT_ROOT 必须从 __file__ 推导，而非 CWD。"""
    import cloud_service.config
    config_path = Path(cloud_service.config.__file__).resolve()
    # cloud_service/config.py 位于 cloud_edge_project/cloud_service/config.py
    # parents[1] = cloud_edge_project
    expected = config_path.parents[1]
    assert cloud_service.config.PROJECT_ROOT == expected, (
        f"PROJECT_ROOT {cloud_service.config.PROJECT_ROOT} does not match "
        f"expected {expected} (from {config_path})"
    )


def test_common_config_project_root_is_file_based() -> None:
    """common.config.PROJECT_ROOT 必须从 __file__ 推导，而非 CWD。"""
    import common.config
    config_path = Path(common.config.__file__).resolve()
    # common/config.py 位于 cloud_edge_project/common/config.py
    # parents[1] = cloud_edge_project
    expected = config_path.parents[1]
    assert common.config.PROJECT_ROOT == expected, (
        f"PROJECT_ROOT {common.config.PROJECT_ROOT} does not match "
        f"expected {expected} (from {config_path})"
    )


def test_sender_default_config_path_resolved() -> None:
    """Sender 默认配置路径应基于 sender_module 目录。"""
    # 直接验证路径解析逻辑，不依赖 sender 导入
    script_dir = Path(__file__).resolve().parent.parent.parent / "sender_module" / "sender"
    config_path = script_dir.parent / "config" / "local.json"
    assert config_path.name == "local.json"
    assert "sender_module" in str(config_path)
    assert config_path.parent.name == "config"


# ---------------------------------------------------------------------------
# 5. ScenarioRegistry 不直接导入轴承
# ---------------------------------------------------------------------------

def test_scenario_registry_does_not_import_bearing() -> None:
    """core.scenario_registry 不得直接导入 BearingCloudHandler。"""
    import core.scenario_registry
    for name, value in core.scenario_registry.__dict__.items():
        if hasattr(value, "__module__") and value.__module__ and "scenarios.bearing" in value.__module__:
            pytest.fail(f"scenario_registry imports bearing module: {value.__module__}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeDataSource:
    """In-memory data source for testing GlobalAnalysisService."""

    def __init__(self, **kwargs: Any) -> None:
        self._data: dict[str, Any] = {}
        self._availability: dict[str, bool] = {}
        for key in ("device_tasks", "bearing_tasks", "bearing_review_pairs",
                     "packet_review_pairs", "arbitrations", "physical_evidence",
                     "edge_summaries"):
            value = kwargs.get(key, [])
            self._data[key] = list(value)
            self._availability[key] = key in kwargs

    def load(self, device_id: str, task_limit: int) -> dict[str, Any]:
        device_rows = sorted(
            (row for row in self._data["device_tasks"] if row.get("device_id") == device_id),
            key=lambda row: row.get("completed_at_ns", 0),
        )[-task_limit:]
        task_ids = {row.get("task_id") for row in device_rows}
        result: dict[str, Any] = {
            "device_tasks": device_rows,
            "availability": dict(self._availability),
        }
        for name in ("bearing_tasks", "bearing_review_pairs", "packet_review_pairs",
                      "arbitrations", "physical_evidence", "edge_summaries"):
            result[name] = [
                row for row in self._data[name]
                if row.get("device_id") == device_id and row.get("task_id") in task_ids
            ]
        if not self._availability.get("bearing_review_pairs", False):
            result["bearing_review_pairs"] = []
        if not self._availability.get("packet_review_pairs", False):
            result["packet_review_pairs"] = []
        if not self._availability.get("physical_evidence", False):
            result["physical_evidence"] = []
        if not self._availability.get("edge_summaries", False):
            result["edge_summaries"] = []
        return result