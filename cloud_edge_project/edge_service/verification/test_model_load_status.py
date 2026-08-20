# -*- coding: utf-8 -*-
"""AUD-01 回归：模型加载状态必须反映真实生命周期。

H5 生产模型（deployment_status="production"、无 estimator）在初始化成功时
必须上报 LOADED；未完成初始化的对象必须上报 ERROR，不得误报 LOADED。
"""
from types import SimpleNamespace

from edge_runtime.coordinator import EdgeRuntimeCoordinator


def _status_for(fallback) -> str:
    host = SimpleNamespace(pipeline=SimpleNamespace(fallback=fallback))
    return EdgeRuntimeCoordinator._model_load_status(host)


def test_h5_production_ready_reports_loaded():
    """Case 1: H5 production、无 estimator、初始化成功 => LOADED。"""
    fallback = SimpleNamespace(
        deployment_status="production",
        model_version="distilled_h5_kd_fold3_a9f20442",
        ready=True,
    )
    assert _status_for(fallback) == "LOADED"


def test_h5_production_not_ready_reports_error():
    """Case 2: H5 初始化失败（ready=False，未完成加载的对象）=> ERROR。"""
    fallback = SimpleNamespace(
        deployment_status="production",
        model_version="distilled_h5_kd_fold3_a9f20442",
        ready=False,
    )
    assert _status_for(fallback) == "ERROR"


def test_legacy_estimator_model_reports_loaded():
    """Case 3: 旧 estimator 模型（无 ready 标记）=> LOADED。"""
    fallback = SimpleNamespace(deployment_status="evaluation_only", estimator=object())
    assert _status_for(fallback) == "LOADED"


def test_built_in_rule_reports_loaded():
    """Case 4: built_in_rule => LOADED。"""
    fallback = SimpleNamespace(deployment_status="built_in_rule")
    assert _status_for(fallback) == "LOADED"


def test_unknown_production_model_without_ready_reports_error():
    """Case 5: 未知模型（production 但无 ready、无 estimator）=> 不得误报 LOADED。"""
    fallback = SimpleNamespace(deployment_status="production")
    assert _status_for(fallback) == "ERROR"


def test_missing_fallback_reports_unloaded():
    """附：无 fallback（模型未装配）=> UNLOADED。"""
    host = SimpleNamespace(pipeline=SimpleNamespace(fallback=None))
    assert EdgeRuntimeCoordinator._model_load_status(host) == "UNLOADED"
