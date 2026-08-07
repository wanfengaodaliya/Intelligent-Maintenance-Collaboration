#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边缘逐包处理的本地演示服务器（stdlib HTTP，无额外依赖）。"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np


_REPO = Path(__file__).resolve().parents[1]
_STATIC = _REPO / "demo" / "edge_frontend"
for _path in (_REPO / "src", _REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from edge_perception import EdgePerception, PerceptionInvocationContext  # noqa: E402
from edge_task_ingress import INGRESS_ACCEPTED, EdgeTaskIngress, TaskIngressConfig  # noqa: E402
from scripts.minimal_local_loop import (  # noqa: E402
    PACKET_COUNT,
    _EDGE_NODE_ID,
    _SENDER_ID,
    _dispatch,
    _model_pipeline,
    _packet,
    _perception_config,
    _signals,
    _validation_cache,
)


class DemoRuntime:
    """串行推进一个 80 包开发任务，并保留每包的四阶段快照。"""

    def __init__(self, model_mode: str):
        self.model_mode = model_mode
        self._lock = threading.Lock()
        self._records: list[Any] = []
        self._packet_results: list[Any] = []
        self._sequence = 0
        self._history: list[dict[str, Any]] = []
        self._packets: list[dict[str, Any]] = []
        self._signals = _signals()
        self._pipeline, health, readiness = _model_pipeline(
            model_mode, self._records, self._packet_results
        )
        self.model_service_health = health.ok
        self.model_service_readiness = readiness.ok if readiness is not None else None
        self._cache = _validation_cache()
        self._ingress = EdgeTaskIngress(TaskIngressConfig(_EDGE_NODE_ID), self._cache)
        self._perception = EdgePerception(_perception_config())
        ack = self._ingress.register_task(_dispatch())
        if ack.ack_status != "ACCEPTED":
            raise RuntimeError(f"演示任务注册失败: {ack.reason_code}")
        self._pipeline.start()

    def close(self) -> None:
        self._pipeline.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_mode": self.model_mode,
                "model_service_health": self.model_service_health,
                "model_service_readiness": self.model_service_readiness,
                "processed_packets": self._sequence,
                "packet_count": PACKET_COUNT,
                "complete": self._sequence >= PACKET_COUNT,
                "history": copy.deepcopy(self._history),
            }

    def packet(self, sequence: int) -> dict[str, Any] | None:
        with self._lock:
            if sequence < 1 or sequence > len(self._packets):
                return None
            return copy.deepcopy(self._packets[sequence - 1])

    def next_packet(self) -> dict[str, Any]:
        with self._lock:
            if self._sequence >= PACKET_COUNT:
                return {"complete": True, "packet_count": PACKET_COUNT}

            sequence = self._sequence + 1
            raw = _packet(sequence, self._signals)
            ingress = self._ingress.receive_packet(raw)
            if ingress.status != INGRESS_ACCEPTED or ingress.validated_packet is None:
                raise RuntimeError(f"数据接入失败: {ingress.error_code}")

            context = PerceptionInvocationContext(
                edge_node_id=_EDGE_NODE_ID,
                perception_received_at_ns=ingress.received_at_ns,
            )
            downsampled = self._perception.downsample(ingress.validated_packet, context)
            if not downsampled.status.success or downsampled.payload is None:
                raise RuntimeError(f"降采样失败: {downsampled.status.error_code}")
            perceived = self._perception.perceive(downsampled.payload, context)
            if not perceived.status.success or perceived.payload is None:
                raise RuntimeError(f"感知失败: {perceived.status.error_code}")

            record_index = len(self._records)
            result_index = len(self._packet_results)
            request_id = self._pipeline.ingest(_SENDER_ID, perceived.payload)
            if not self._pipeline.wait_idle(timeout_s=6.0):
                raise RuntimeError("模型任务未在 6 秒内结束")
            if len(self._records) != record_index + 1:
                raise RuntimeError("模型运行记录数量异常")
            if len(self._packet_results) != result_index + 1:
                raise RuntimeError("最终 EdgeResult 数量异常")

            record = self._records[record_index]
            packet_result = self._packet_results[result_index]
            if record.request_id != request_id:
                raise RuntimeError("模型运行记录与当前包 request_id 不一致")

            snapshot = {
                "sequence_number": sequence,
                "request_id": request_id,
                "received_at_ns": str(ingress.received_at_ns),
                "input": _input_snapshot(raw),
                "perception": _perception_snapshot(perceived.payload),
                "model": record.as_dict(),
                "edge_result": packet_result.as_dict(),
            }
            self._sequence = sequence
            self._packets.append(snapshot)
            self._history.append(_history_item(snapshot))
            return {"complete": self._sequence >= PACKET_COUNT, "packet": snapshot}


class RuntimeHolder:
    def __init__(self, model_mode: str):
        self.model_mode = model_mode
        self._lock = threading.Lock()
        self.runtime = DemoRuntime(model_mode)

    def reset(self) -> dict[str, Any]:
        with self._lock:
            old = self.runtime
            old.close()
            self.runtime = DemoRuntime(self.model_mode)
            return self.runtime.status()

    def close(self) -> None:
        with self._lock:
            self.runtime.close()


class DemoHandler(SimpleHTTPRequestHandler):
    holder: RuntimeHolder

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_STATIC), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[edge_demo] %s\n" % (fmt % args))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/status":
            self._send_json(200, self.holder.runtime.status())
            return
        if path.startswith("/api/packets/"):
            try:
                sequence = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(400, {"error": "invalid_sequence"})
                return
            packet = self.holder.runtime.packet(sequence)
            if packet is None:
                self._send_json(404, {"error": "packet_not_found"})
            else:
                self._send_json(200, {"packet": packet})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/api/next":
                self._send_json(200, self.holder.runtime.next_packet())
            elif path == "/api/reset":
                self._send_json(200, self.holder.reset())
            else:
                self._send_json(404, {"error": "not_found"})
        except Exception as exc:  # demo boundary: return actionable failure to the page
            self._send_json(500, {"error": type(exc).__name__, "detail": str(exc)})


def _input_snapshot(packet: dict[str, Any]) -> dict[str, Any]:
    result = {
        field: packet[field]
        for field in (
            "device_id",
            "bearing_id",
            "sender_id",
            "task_id",
            "packet_id",
            "sequence_number",
        )
    }
    result["end_generate_timestamp_ns"] = str(packet["end_generate_timestamp_ns"])
    result["data"] = {}
    for name, channel in packet["data"].items():
        if not isinstance(channel, dict):
            result["data"][name] = channel
            continue
        values = np.asarray(channel["values"], dtype=np.float64)
        preview_size = 128 if values.size >= 800 else 32
        result["data"][name] = {
            key: value for key, value in channel.items() if key != "values"
        }
        result["data"][name]["values_preview"] = [
            round(float(value), 6) for value in values[:preview_size]
        ]
        result["data"][name]["preview_count"] = preview_size
    return result


def _perception_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["feature_generated_at_ns"] = str(result["feature_generated_at_ns"])
    result["end_generate_timestamp_ns"] = str(result["end_generate_timestamp_ns"])
    return result


def _history_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    model = snapshot["model"]
    edge = snapshot["edge_result"]
    vibration = snapshot["perception"]["features"]["vibration"]
    return {
        "sequence_number": snapshot["sequence_number"],
        "packet_id": edge["packet_id"],
        "edge_result": edge["edge_result"],
        "edge_risk_level": edge["edge_risk_level"],
        "confidence": edge["confidence"],
        "execution_mode": model["execution_mode"],
        "total_latency_ms": model["total_latency_ms"],
        "vibration_rms": vibration["rms"],
        "kurtosis": vibration["kurtosis"],
    }


def make_server(host: str, port: int, holder: RuntimeHolder) -> ThreadingHTTPServer:
    DemoHandler.holder = holder
    return ThreadingHTTPServer((host, port), DemoHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="边缘逐包处理前端演示")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--model-mode", choices=("fallback", "real"), default="fallback")
    args = parser.parse_args()

    holder = RuntimeHolder(args.model_mode)
    server = make_server(args.host, args.port, holder)
    print(f"边缘演示页面: http://{args.host}:{args.port}")
    print(f"模型路线: {args.model_mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        holder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
