#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 Windows—WSL 逐包语义冒烟。

本脚本为了验证请求/结果一一对应，会等待上一包完成后再提交下一包。
它不验证 20 包/秒吞吐；吞吐由 vLLM 专用压测验证。
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
for _path in (_SRC, _REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from edge_model.code_fallback import TestRuleRunner  # noqa: E402
from edge_model.config import EdgeModelConfig, ModelClientConfig  # noqa: E402
from edge_model.model_client import ModelClient  # noqa: E402
from edge_model.pipeline import EdgeModelPipeline  # noqa: E402
from model_input_contract import model_input_probe  # noqa: E402


def make_perception(index: int, task_id: str = "task-smoke") -> dict:
    kurtosis = [3.1, 3.2, 6.1, 6.3, 12.0][index % 5]
    result = copy.deepcopy(model_input_probe())
    result.update({
        "device_id": "device-smoke",
        "bearing_id": "bearing-smoke",
        "task_id": task_id,
        "packet_id": "pkt_%06d" % index,
        "sender_id": "sender-smoke",
        "sequence_number": index + 1,
        "end_generate_timestamp_ns": 1_700_000_000_000_000_000 + index * 50_000_000,
        "feature_generated_at_ns": 1_700_000_000_010_000_000 + index * 50_000_000,
    })
    result["features"]["vibration"].update({
        "rms": 0.3 + index * 0.001,
        "kurtosis": kurtosis,
    })
    result["features"]["current_relationship"]["current_imbalance_ratio"] = (
        0.02 + index * 0.001
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", default="http://127.0.0.1:8001")
    parser.add_argument("--packets", type=int, default=20)
    args = parser.parse_args()

    cfg = EdgeModelConfig()
    cfg.model_client = ModelClientConfig(base_url=args.service_url)
    client = ModelClient(cfg.model_client)

    health = client.health()
    readiness = client.readiness() if health.ok else None
    if not health.ok or readiness is None or not readiness.ok:
        detail = readiness.detail if readiness is not None else health.detail
        print("FAIL 模型服务未就绪:", detail)
        raise SystemExit(1)

    records, packets = [], []
    pipeline = EdgeModelPipeline(
        cfg,
        client,
        TestRuleRunner(cfg.fallback.rule_version),
        on_run_record=records.append,
        on_packet_result=packets.append,
    )
    pipeline.start()
    try:
        for index in range(args.packets):
            pipeline.ingest("sender-smoke", make_perception(index))
            if not pipeline.wait_idle(timeout_s=10.0):
                raise RuntimeError("第%d包未在10秒内完成" % (index + 1))
    finally:
        pipeline.stop()

    ok = True
    if len(records) != args.packets or len(packets) != args.packets:
        print("FAIL 期望%d次运行记录和包结果，实际%d/%d" %
              (args.packets, len(records), len(packets)))
        ok = False
    if len({record.request_id for record in records}) != args.packets:
        print("FAIL request_id不唯一")
        ok = False
    if any(record.execution_mode != "LOCAL_MODEL" for record in records):
        print("FAIL 存在非真实模型路线:",
              [(record.packet_id, record.execution_mode, record.fallback_reason)
               for record in records if record.execution_mode != "LOCAL_MODEL"])
        ok = False
    if [packet.sequence_number for packet in packets] != list(range(1, args.packets + 1)):
        print("FAIL 包顺序或身份不完整")
        ok = False
    if any(packet.edge.model_version.startswith("edge_rule") for packet in packets):
        print("FAIL 真实模型冒烟中出现代码降级结果")
        ok = False

    print("包数:", len(packets))
    print("独立模型运行记:", len(records))
    print("唯一request_id:", len({record.request_id for record in records}))
    print("结论:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
