# 这个文件负责把调度器包装成 HTTP 服务
"""HTTP API for sender-to-edge node assignment."""

from __future__ import annotations

import atexit
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

try:
    from common.config import load_config
    from common.schemas import (
        ContractError,
        is_v01_schedule_request,
        require_mapping,
        validate_schedule_request_v01,
    )
except ImportError:  # Allows running this file directly: python scheduler/api.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.config import load_config
    from common.schemas import (
        ContractError,
        is_v01_schedule_request,
        require_mapping,
        validate_schedule_request_v01,
    )

try:
    from .assignment_scheduler import AssignmentError, AssignmentScheduler
    from .node_registry import NodeRegistry, RegistryError
    from .rule_scheduler import decide_schedule_v01
    from .task_repository import TaskRepository, TaskRepositoryError
except ImportError:  # Allows running this file directly: python api.py
    from assignment_scheduler import AssignmentError, AssignmentScheduler
    from node_registry import NodeRegistry, RegistryError
    from rule_scheduler import decide_schedule_v01
    from task_repository import TaskRepository, TaskRepositoryError

try:
    from fastapi import APIRouter, Body, FastAPI
    from fastapi.responses import JSONResponse
except ImportError:
    APIRouter = None
    Body = None
    FastAPI = None
    JSONResponse = None


node_registry = NodeRegistry()
task_repository = TaskRepository()
scheduler = AssignmentScheduler(node_registry, task_repository)
node_registry.start_monitor()
atexit.register(node_registry.stop_monitor)


# 同时兼容文档 6.2 的 V0.1 调度请求和发送器的任务级节点分配请求。
def decide(request: Any) -> dict[str, Any]:
    payload = require_mapping(request, "ScheduleRequest")
    if is_v01_schedule_request(payload):
        return decide_schedule_v01(validate_schedule_request_v01(payload))
    return scheduler.decide(payload).to_dict()


def _is_v01_schedule_request(request: Mapping[str, Any]) -> bool:
    return is_v01_schedule_request(request)


# 接收边缘节点的实时状态报告
def update_edge_node_status(request: dict[str, Any]) -> dict[str, Any]:
    return node_registry.update_status(request)


# 接收网络模块提供的发送器到边缘节点链路快照
def update_link_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    return node_registry.update_link(request)


def update_network_report(
    request: dict[str, Any], *, selected_link_ids: set[str] | None = None
) -> dict[str, Any]:
    """Adapt a simulator batch report to the existing formal link snapshots."""

    sequence = request.get("report_sequence")
    measured_at_ns = request.get("generated_at_ns")
    links = request.get("links")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(measured_at_ns, int)
        or isinstance(measured_at_ns, bool)
        or measured_at_ns < 1
        or not isinstance(links, list)
    ):
        raise RegistryError("INVALID_LINK_SNAPSHOT", "invalid network simulator report")
    for item in links:
        if not isinstance(item, dict):
            continue
        link_id = item.get("link_id")
        if selected_link_ids is not None and link_id not in selected_link_ids:
            continue
        if (
            item.get("protocol") != "mqtt"
            or not isinstance(item.get("sender_id"), str)
            or not isinstance(item.get("edge_id"), str)
        ):
            continue
        try:
            node_registry.update_link(_simulator_link_snapshot(item, measured_at_ns))
        except RegistryError as error:
            if error.code != "UNREGISTERED_EDGE_NODE":
                raise
    return {"accepted": True, "report_sequence": sequence}


def _simulator_link_snapshot(item: dict[str, Any], measured_at_ns: int) -> dict[str, Any]:
    available = item.get("available") is True and item.get("last_apply_success") is True
    latency = _non_negative_network_value(item.get("latency_ms")) if available else 0.0
    jitter = _non_negative_network_value(item.get("jitter_ms")) if available else 0.0
    bandwidth_kbps = _non_negative_network_value(item.get("bandwidth_kbps")) if available else 0.0
    loss_percent = min(
        _non_negative_network_value(item.get("packet_loss_percent")) if available else 100.0,
        100.0,
    )
    return {
        "sender_id": item["sender_id"],
        "edge_node_id": item["edge_id"],
        "measured_at_ns": measured_at_ns,
        "rtt_ms_avg": latency,
        "rtt_ms_p95": latency + 2.0 * jitter,
        "jitter_ms": jitter,
        "available_throughput_mbps": bandwidth_kbps / 1000.0,
        "mqtt_publish_success_rate": 1.0 - loss_percent / 100.0 if available else 0.0,
    }


def _non_negative_network_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RegistryError("INVALID_LINK_SNAPSHOT", "invalid network simulator metric")
    return float(value)


# 保存任务最终执行结果
def save_task_result(request: dict[str, Any]) -> dict[str, Any]:
    return scheduler.save_result(request)


def health() -> dict[str, Any]:
    scheduler_config = _scheduler_config()
    return {
        "service": "scheduler_service",
        "node_id": "scheduler_1",
        "status": "ok",
        "model_loaded": True,
        "model_backend": "rule",
        "device": "cpu",
        "port": scheduler_config["port"],
        "edge_nodes": node_registry.status_counts(),
    }


def _error_payload(error: Exception) -> tuple[int, dict[str, Any]]:
    if isinstance(error, ContractError):
        return 400, {
            "error_code": error.code,
            "message": error.message,
        }
    if isinstance(error, (AssignmentError, TaskRepositoryError)):
        return error.status_code, {
            "error_code": error.code,
            "message": error.message,
        }
    if isinstance(error, RegistryError):
        return 400, {
            "error_code": error.code,
            "message": error.message,
        }
    if isinstance(error, sqlite3.OperationalError):
        return 503, {
            "error_code": "SCHEDULER_BUSY",
            "message": "scheduler storage is temporarily busy",
        }
    return 500, {
        "error_code": "SCHEDULER_INTERNAL_ERROR",
        "message": str(error),
    }


if APIRouter is not None:
    router = APIRouter(prefix="/scheduler", tags=["scheduler"])

    @router.post("/decide", response_model=None)
    def decide_endpoint(request: Any = Body(default=None)) -> dict[str, Any] | JSONResponse:
        try:
            return decide(request)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/edge-nodes/status", response_model=None)
    def edge_node_status_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        try:
            return update_edge_node_status(request)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/link-snapshots", response_model=None)
    def link_snapshot_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        try:
            return update_link_snapshot(request)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/network-reports/{link_id}", response_model=None)
    def network_report_endpoint(
        link_id: str, request: dict[str, Any]
    ) -> dict[str, Any] | JSONResponse:
        try:
            return update_network_report(
                request,
                selected_link_ids={link_id},
            )
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/tasks/result", response_model=None)
    def task_result_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        try:
            return save_task_result(request)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    app = FastAPI(title="Edge Node Assignment Scheduler")
    app.include_router(router)

    @app.get("/health")
    def health_endpoint() -> dict[str, Any]:
        return health()
else:
    router = None
    app = None


class SchedulerRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, health())
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        handlers = {
            "/scheduler/decide": decide,
            "/scheduler/edge-nodes/status": update_edge_node_status,
            "/scheduler/link-snapshots": update_link_snapshot,
            "/scheduler/tasks/result": save_task_result,
        }
        report_prefix = "/scheduler/network-reports/"
        if parsed.path.startswith(report_prefix):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                request = json.loads(body) if body else {}
                link_id = unquote(parsed.path[len(report_prefix):])
                if not link_id or "/" in link_id:
                    raise RegistryError("INVALID_LINK_SNAPSHOT", "invalid link_id")
                self._send_json(
                    200,
                    update_network_report(request, selected_link_ids={link_id}),
                )
            except json.JSONDecodeError:
                self._send_json(400, {"error_code": "INVALID_JSON", "message": "invalid JSON body"})
            except Exception as error:
                status_code, payload = _error_payload(error)
                self._send_json(status_code, payload)
            return
        handler = handlers.get(parsed.path)
        if handler is None:
            self._send_json(404, {"detail": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            request = json.loads(body) if body else {}
            self._send_json(200, handler(request))
        except json.JSONDecodeError:
            self._send_json(400, {"error_code": "INVALID_JSON", "message": "invalid JSON body"})
        except Exception as error:
            status_code, payload = _error_payload(error)
            self._send_json(status_code, payload)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run(host: str | None = None, port: int | None = None) -> None:
    scheduler_config = _scheduler_config()
    selected_host = scheduler_config["host"] if host is None else host
    selected_port = scheduler_config["port"] if port is None else port
    server = ThreadingHTTPServer((selected_host, selected_port), SchedulerRequestHandler)
    print(f"scheduler service running at http://{selected_host}:{selected_port}")
    server.serve_forever()


def _scheduler_config() -> Mapping[str, Any]:
    return load_config()["services"]["scheduler"]


if __name__ == "__main__":
    run()
