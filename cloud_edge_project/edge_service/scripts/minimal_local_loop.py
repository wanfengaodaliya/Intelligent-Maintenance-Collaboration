#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成数据驱动的本地最小技术闭环。

真实执行任务接入、严格校验、环形缓存、降采样、感知和模型流水线。
fallback 模式使用真实 ModelClient 连接一个不可用本地端口，再显式进入
edge_rule_test_v1；它不使用 MockModel，也不代表真实业务诊断。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
for _path in (_SRC, _REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from edge_diagnosis import (  # noqa: E402
    MockDiagnosticModel,
    RandomForestDiagnosticModel,
)
from edge_model.config import EdgeModelConfig, ModelClientConfig  # noqa: E402
from edge_model.contracts import (  # noqa: E402
    EXECUTION_CODE_FALLBACK,
    EXECUTION_LOCAL_MODEL,
)
from edge_model.model_client import ModelClient  # noqa: E402
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from edge_perception import (  # noqa: E402
    ConstantDetectionConfig,
    EdgePerception,
    PerceptionConfig,
    PerceptionInvocationContext,
    file_sha256,
)
from edge_task_ingress import (  # noqa: E402
    INGRESS_ACCEPTED,
    EdgeTaskIngress,
    TaskIngressConfig,
)
from edge_validation_cache import (  # noqa: E402
    EdgeValidationCache,
    ValidationCacheConfig,
)


DATA_SOURCE = "synthetic_development_test"
PACKET_COUNT = 80
PACKET_PERIOD_NS = 50_000_000
_TASK_ID = "task-minimal-local-loop"
_DEVICE_ID = "device-synthetic-1"
_BEARING_ID = "bearing-synthetic-1"
_SENDER_ID = "sender-synthetic-1"
_EDGE_NODE_ID = "edge-1"
_FIR_ASSET = _SRC / "edge_perception" / "assets" / "fir_64k_to_16k_369.txt"


class RealModelUnavailable(RuntimeError):
    pass


def _perception_config() -> PerceptionConfig:
    source = "development_test"
    version = "minimal-loop-v1"
    return PerceptionConfig(
        profile="development_test",
        fir_coefficients_path=_FIR_ASSET,
        fir_sha256=file_sha256(_FIR_ASSET),
        fir_asset_source="development_test",
        fir_asset_version="dev-v1",
        running_speed_threshold_rpm=100.0,
        running_speed_threshold_source=source,
        running_speed_threshold_version=version,
        constant_detection={
            "vibration": ConstantDetectionConfig(True, 1e-9, source, version),
            "phase_current_1_A": ConstantDetectionConfig(True, 1e-9, source, version),
            "phase_current_2_A": ConstantDetectionConfig(True, 1e-9, source, version),
        },
        feature_zero_rms_threshold=1e-10,
        feature_zero_power_threshold=1e-20,
        current_relationship_zero_rms_threshold=1e-10,
        numerical_threshold_source=source,
        numerical_threshold_version=version,
        feature_extractor_version="perception-minimal-loop-v1",
        runtime_dependencies={"numpy": np.__version__},
        absolute_tolerance=1e-12,
        relative_tolerance=1e-9,
    )


def _validation_cache() -> EdgeValidationCache:
    return EdgeValidationCache(
        ValidationCacheConfig(
            raw_cache_retention_seconds=60.0,
            max_receive_rate_per_sender=20.0,
            context_queue_capacity_per_sender=1200,
            raw_cache_capacity_per_sender=1200,
            context_before_packet_count=20,
            cache_cleanup_interval_seconds=1.0,
            hard_value_ranges={},
        )
    )


def _dispatch() -> dict:
    return {
        "task_id": _TASK_ID,
        "target_edge_node_id": _EDGE_NODE_ID,
        "task_type": "BEARING_EDGE_INFERENCE",
        "input_ref": {
            "device_id": _DEVICE_ID,
            "expected_bearing_ids": [_BEARING_ID],
            "assigned_bearings": [
                {
                    "bearing_id": _BEARING_ID,
                    "sender_id": _SENDER_ID,
                    "expected_packet_count": PACKET_COUNT,
                }
            ],
        },
        "dispatched_at_ns": time.time_ns(),
    }


def _signals() -> dict[str, np.ndarray]:
    high_t = np.arange(3200, dtype=np.float64) / 64000.0
    low_t = np.arange(200, dtype=np.float64) / 4000.0
    return {
        "vibration": 2.0 * np.sin(2.0 * np.pi * 1000.0 * high_t),
        "phase_current_1_A": 10.0 + 2.0 * np.sin(2.0 * np.pi * 200.0 * high_t),
        "phase_current_2_A": 10.0 + 1.8 * np.sin(2.0 * np.pi * 200.0 * high_t),
        "shaft_speed_rpm": np.full(200, 1500.0),
        "load_torque_nm": 10.0 + 0.1 * np.sin(2.0 * np.pi * 10.0 * low_t),
        "bearing_radial_load_n": 1000.0 + 5.0 * np.sin(2.0 * np.pi * 5.0 * low_t),
    }


def _channel(values: np.ndarray, rate: int, unit: str | None = None) -> dict:
    result = {
        "sample_rate_hz": rate,
        "sample_count": int(values.size),
        "values": values,
    }
    if unit is not None:
        result["unit"] = unit
    return result


def _packet(sequence: int, signals: dict[str, np.ndarray]) -> dict:
    generated_at = 1_700_000_000_000_000_000 + (sequence - 1) * PACKET_PERIOD_NS
    return {
        "device_id": _DEVICE_ID,
        "bearing_id": _BEARING_ID,
        "sender_id": _SENDER_ID,
        "task_id": _TASK_ID,
        "packet_id": f"synthetic-packet-{sequence:03d}",
        "sequence_number": sequence,
        "end_generate_timestamp_ns": generated_at,
        "data": {
            "vibration": _channel(signals["vibration"], 64000, "mm/s"),
            "phase_current_1_A": _channel(
                signals["phase_current_1_A"], 64000, "A"
            ),
            "phase_current_2_A": _channel(
                signals["phase_current_2_A"], 64000, "A"
            ),
            "shaft_speed_rpm": _channel(signals["shaft_speed_rpm"], 4000),
            "load_torque_nm": _channel(signals["load_torque_nm"], 4000),
            "bearing_radial_load_n": _channel(
                signals["bearing_radial_load_n"], 4000
            ),
            "bearing_module_temperature_c": 46.3,
        },
    }


def _model_pipeline(
    mode: str,
    records: list,
    packet_results: list,
    model_path: Path | None = None,
    metadata_path: Path | None = None,
):
    cfg = EdgeModelConfig()
    cfg.diagnostic_backend = (
        "http"
        if mode == "real"
        else "rf_50ms_integration"
        if mode == "rf-integration"
        else "mock"
    )
    cfg.fallback.allow_test_rule = True
    if mode == "fallback":
        cfg.model_client = ModelClientConfig(
            base_url="http://127.0.0.1:1",
            connect_timeout_ms=100,
            read_timeout_ms=500,
        )
    client = ModelClient(cfg.model_client)
    health = client.health()
    readiness = client.readiness() if health.ok else None
    if mode == "real" and (not health.ok or readiness is None or not readiness.ok):
        detail = readiness.detail if readiness is not None else health.detail
        raise RealModelUnavailable(detail)
    if mode == "fallback" and health.ok:
        raise RuntimeError("fallback测试端口意外存在可用模型服务")
    if mode == "rf-integration":
        if model_path is None or metadata_path is None:
            raise ValueError("rf-integration需要--model-path和--metadata-path")
        runner = RandomForestDiagnosticModel(model_path, metadata_path)
    else:
        runner = MockDiagnosticModel(cfg.fallback.rule_version)
    pipeline = EdgeModelPipeline(
        cfg,
        client,
        runner,
        on_run_record=records.append,
        on_packet_result=packet_results.append,
    )
    return pipeline, health, readiness


def run_minimal_loop(
    mode: str,
    model_path: Path | None = None,
    metadata_path: Path | None = None,
) -> dict:
    records: list = []
    packet_results: list = []
    pipeline, health, readiness = _model_pipeline(
        mode, records, packet_results, model_path, metadata_path
    )
    cache = _validation_cache()
    ingress = EdgeTaskIngress(TaskIngressConfig(_EDGE_NODE_ID), cache)
    perception = EdgePerception(_perception_config())
    ack = ingress.register_task(_dispatch())
    _require(ack.ack_status == "ACCEPTED", f"任务拒绝: {ack.reason_code}")

    accepted = downsampled_count = perceived_count = 0
    signals = _signals()
    pipeline.start()
    try:
        for sequence in range(1, PACKET_COUNT + 1):
            ingress_result = ingress.receive_packet(_packet(sequence, signals))
            _require(
                ingress_result.status == INGRESS_ACCEPTED
                and ingress_result.error_code is None
                and ingress_result.validated_packet is not None,
                f"第{sequence}包接入失败: {ingress_result.error_code}",
            )
            accepted += 1
            context = PerceptionInvocationContext(
                edge_node_id=_EDGE_NODE_ID,
                perception_received_at_ns=ingress_result.received_at_ns,
            )
            downsampled = perception.downsample(
                ingress_result.validated_packet, context
            )
            _require(
                downsampled.status.success,
                f"第{sequence}包降采样失败: {downsampled.status.error_code}",
            )
            _require(
                downsampled.payload["data"]["vibration"]["sample_count"] == 800,
                f"第{sequence}包降采样点数错误",
            )
            downsampled_count += 1
            perceived = perception.perceive(downsampled.payload, context)
            _require(
                perceived.status.success,
                f"第{sequence}包感知失败: {perceived.status.error_code}",
            )
            _require(
                math.isfinite(perceived.payload["features"]["vibration"]["rms"]),
                f"第{sequence}包振动RMS非法",
            )
            perceived_count += 1
            pipeline.ingest(_SENDER_ID, perceived.payload)
            _require(pipeline.wait_idle(timeout_s=5), "模型队列未在5秒内清空")
    finally:
        pipeline.stop()

    task = ingress.task_snapshot(_TASK_ID)
    bearing = task.bearing_task_records[_BEARING_ID]
    slots = cache.context_snapshot(_SENDER_ID)
    execution_modes = Counter(record.execution_mode for record in records)
    fallback_reasons = Counter(
        record.fallback_reason for record in records if record.fallback_reason
    )
    versions = Counter(result.edge.model_version for result in packet_results)

    _require(accepted == PACKET_COUNT, "接入成功包数不是80")
    _require(downsampled_count == PACKET_COUNT, "降采样成功包数不是80")
    _require(perceived_count == PACKET_COUNT, "感知成功包数不是80")
    _require(len(slots) == PACKET_COUNT, "环形缓存上下文位置数不是80")
    _require(all(slot.cache_status == "AVAILABLE" for slot in slots), "存在不可用缓存位置")
    _require(bearing.data_completeness == "COMPLETE", "轴承任务数据不完整")
    _require(bearing.missing_packet_count == 0, "轴承任务出现缺失包")
    _require(bearing.summary_generated_count == 0, "当前阶段不应生成包摘要")
    _require(len(records) == PACKET_COUNT, "包级模型运行记录数量不是80")
    _require(len({record.request_id for record in records}) == PACKET_COUNT,
             "包级模型请求request_id不唯一")
    _require(len(packet_results) == PACKET_COUNT, "PacketResult数量不是80")
    _require(
        {result.sequence_number for result in packet_results}
        == set(range(1, PACKET_COUNT + 1)),
        "PacketResult序号不完整",
    )
    expected_mode = (
        EXECUTION_CODE_FALLBACK
        if mode in {"fallback", "rf-integration"}
        else EXECUTION_LOCAL_MODEL
    )
    _require(
        execution_modes == Counter({expected_mode: PACKET_COUNT}),
        f"执行路线不符合{mode}模式: {dict(execution_modes)}",
    )
    if mode == "real":
        _require(not fallback_reasons, f"真实模型发生降级: {dict(fallback_reasons)}")
        _require(
            all(not version.startswith("edge_rule_test") for version in versions),
            "真实模型结果包含测试规则版本",
        )
    elif mode == "rf-integration":
        _require(
            set(versions) == {"bearing-rf-50ms-integration-only-v1"},
            "RF联调结果未使用明确的临时模型版本",
        )
    else:
        _require(
            all(version.startswith("bearing_diagnosis_mock") for version in versions),
            "代码替代结果未明确使用测试规则版本",
        )

    return {
        "status": "PASS",
        "scope": "packet_level_technical_loop",
        "data_source": DATA_SOURCE,
        "model_mode_requested": mode,
        "model_service_health": health.ok,
        "model_service_readiness": readiness.ok if readiness is not None else None,
        "task_ack": ack.ack_status,
        "ingress_accepted_packets": accepted,
        "cache_available_slots": len(slots),
        "downsampled_packets": downsampled_count,
        "perceived_packets": perceived_count,
        "bearing_data_completeness": bearing.data_completeness,
        "missing_packet_count": bearing.missing_packet_count,
        "model_packet_tasks": len(records),
        "unique_model_request_ids": len({record.request_id for record in records}),
        "packet_results": len(packet_results),
        "execution_modes": dict(execution_modes),
        "fallback_reasons": dict(fallback_reasons),
        "model_versions": dict(versions),
        "device_result_generated": False,
        "technical_only": True,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-mode",
        choices=("fallback", "real", "rf-integration"),
        default="fallback",
    )
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--metadata-path", type=Path)
    args = parser.parse_args()
    try:
        report = run_minimal_loop(
            args.model_mode, args.model_path, args.metadata_path
        )
    except RealModelUnavailable as exc:
        report = {
            "status": "BLOCKED",
            "scope": "real_local_model_route",
            "data_source": DATA_SOURCE,
            "reason": "MODEL_SERVICE_NOT_READY",
            "detail": str(exc),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        report = {
            "status": "FAIL",
            "scope": "packet_level_technical_loop",
            "data_source": DATA_SOURCE,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
