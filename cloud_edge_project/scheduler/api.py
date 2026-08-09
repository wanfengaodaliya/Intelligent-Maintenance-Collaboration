# 这个文件负责把调度器包装成 HTTP 服务
"""HTTP API for sender-to-edge node assignment."""

from __future__ import annotations

import atexit
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .assignment_scheduler import AssignmentError, AssignmentScheduler
    from .node_registry import NodeRegistry, RegistryError
    from .task_repository import TaskRepository, TaskRepositoryError
except ImportError:  # Allows running this file directly: python api.py
    from assignment_scheduler import AssignmentError, AssignmentScheduler
    from node_registry import NodeRegistry, RegistryError
    from task_repository import TaskRepository, TaskRepositoryError

try:
    from fastapi import APIRouter, FastAPI
    from fastapi.responses import JSONResponse
except ImportError:
    APIRouter = None
    FastAPI = None
    JSONResponse = None


node_registry = NodeRegistry()
task_repository = TaskRepository()
scheduler = AssignmentScheduler(node_registry, task_repository)
node_registry.start_monitor()
atexit.register(node_registry.stop_monitor)


# 接收发送器的任务级调度请求，返回边缘节点 MQTT Topic
def decide(request: dict[str, Any]) -> dict[str, Any]:
    return scheduler.decide(request).to_dict()


# 接收边缘节点的实时状态报告
def update_edge_node_status(request: dict[str, Any]) -> dict[str, Any]:
    return node_registry.update_status(request)


# 接收网络模块提供的发送器到边缘节点链路快照
def update_link_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    return node_registry.update_link(request)


# 保存任务最终执行结果
def save_task_result(request: dict[str, Any]) -> dict[str, Any]:
    return scheduler.save_result(request)


def health() -> dict[str, Any]:
    return {
        "service": "scheduler_service",
        "node_id": "scheduler_1",
        "status": "ok",
        "model_loaded": True,
        "model_backend": "rule",
        "device": "cpu",
        "port": 8003,
        "edge_nodes": node_registry.status_counts(),
    }


def _error_payload(error: Exception) -> tuple[int, dict[str, Any]]:
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
    def decide_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
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
        handlers = {
            "/scheduler/decide": decide,
            "/scheduler/edge-nodes/status": update_edge_node_status,
            "/scheduler/link-snapshots": update_link_snapshot,
            "/scheduler/tasks/result": save_task_result,
        }
        handler = handlers.get(self.path)
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


def run(host: str = "127.0.0.1", port: int = 8003) -> None:
    server = ThreadingHTTPServer((host, port), SchedulerRequestHandler)
    print(f"scheduler service running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
