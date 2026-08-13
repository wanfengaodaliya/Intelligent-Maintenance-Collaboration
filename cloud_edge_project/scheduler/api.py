"""HTTP API for assignment and package-level cloud review scheduling."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

if __package__ in {None, ""}:
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from common.config import load_config
from common.schemas import (
    ContractError,
    is_v01_schedule_request,
    require_mapping,
    validate_schedule_request_v01,
)

if __package__ in {None, ""}:
    from scheduler.assignment_scheduler import AssignmentError
    from scheduler.deferred_cloud_repository import DeferredCloudError
    from scheduler.node_registry import RegistryError
    from scheduler.packet_router import PacketRouteError
    from scheduler.runtime import SchedulerRuntime
    from scheduler.task_repository import TaskRepositoryError
else:
    from .assignment_scheduler import AssignmentError
    from .deferred_cloud_repository import DeferredCloudError
    from .node_registry import RegistryError
    from .packet_router import PacketRouteError
    from .runtime import SchedulerRuntime
    from .task_repository import TaskRepositoryError

try:
    from fastapi import APIRouter, Body, FastAPI
    from fastapi.responses import JSONResponse
except ImportError:
    APIRouter = None
    Body = None
    FastAPI = None
    JSONResponse = None


default_runtime = SchedulerRuntime()
# Compatibility aliases for callers that imported the previous module globals.
node_registry = default_runtime.node_registry
task_repository = default_runtime.task_repository
scheduler = default_runtime.assignment_scheduler


def decide(request: Any) -> dict[str, Any]:
    payload = require_mapping(request, "ScheduleRequest")
    if is_v01_schedule_request(payload):
        return default_runtime.decide(
            validate_schedule_request_v01(payload),
            v01=True,
        )
    return scheduler.decide(payload).to_dict()


def _is_v01_schedule_request(request: Mapping[str, Any]) -> bool:
    return is_v01_schedule_request(request)


def update_edge_node_status(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.update_edge_node_status(request)


def update_cloud_node_status(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.update_cloud_node_status(request)


def update_link_snapshot(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.update_link_snapshot(request)


def route_packet(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.route_packet(request)


def save_cloud_upload_result(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.save_cloud_upload_result(request)


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
def save_task_result(request: Mapping[str, Any]) -> dict[str, Any]:
    return default_runtime.save_task_result(request)


def health() -> dict[str, Any]:
    scheduler_config = _scheduler_config()
    payload = default_runtime.health()
    payload["port"] = scheduler_config["port"]
    return payload


def _error_payload(error: Exception) -> tuple[int, dict[str, Any]]:
    if isinstance(error, ContractError):
        return 400, {"error_code": error.code, "message": error.message}
    if isinstance(
        error,
        (AssignmentError, TaskRepositoryError, PacketRouteError, DeferredCloudError),
    ):
        return error.status_code, {"error_code": error.code, "message": error.message}
    if isinstance(error, RegistryError):
        return 400, {"error_code": error.code, "message": error.message}
    if isinstance(error, sqlite3.OperationalError):
        return 503, {
            "error_code": "SCHEDULER_BUSY",
            "message": "scheduler storage is temporarily busy",
        }
    return 500, {"error_code": "SCHEDULER_INTERNAL_ERROR", "message": str(error)}


def create_app(runtime: SchedulerRuntime | Any | None = None) -> Any:
    if FastAPI is None:
        return None
    selected = runtime or default_runtime

    @asynccontextmanager
    async def lifespan(_: Any):
        selected.start()
        try:
            yield
        finally:
            selected.stop()

    router = APIRouter(prefix="/scheduler", tags=["scheduler"])

    def endpoint(method_name: str, request: Any) -> dict[str, Any] | JSONResponse:
        try:
            return getattr(selected, method_name)(request)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/decide", response_model=None)
    def decide_endpoint(request: Any = Body(default=None)) -> dict[str, Any] | JSONResponse:
        try:
            payload = require_mapping(request, "ScheduleRequest")
            if is_v01_schedule_request(payload):
                return selected.decide(validate_schedule_request_v01(payload), v01=True)
            return selected.decide(payload)
        except Exception as error:
            status_code, payload = _error_payload(error)
            return JSONResponse(status_code=status_code, content=payload)

    @router.post("/edge-nodes/status", response_model=None)
    def edge_status_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        return endpoint("update_edge_node_status", request)

    @router.post("/cloud-nodes/status", response_model=None)
    def cloud_status_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        return endpoint("update_cloud_node_status", request)

    @router.post("/link-snapshots", response_model=None)
    def link_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        return endpoint("update_link_snapshot", request)

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
        return endpoint("save_task_result", request)

    @router.post("/packet-route", response_model=None)
    def packet_route_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        return endpoint("route_packet", request)

    @router.post("/cloud-upload-results", response_model=None)
    def upload_result_endpoint(request: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        return endpoint("save_cloud_upload_result", request)

    application = FastAPI(
        title="Edge Node Assignment Scheduler",
        lifespan=lifespan,
    )
    application.state.scheduler_router = router
    application.include_router(router)

    @application.get("/health")
    def health_endpoint() -> dict[str, Any]:
        return selected.health()

    return application


app = create_app()
router = app.state.scheduler_router if app is not None else None


class SchedulerRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, health())
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "/scheduler/decide": decide,
            "/scheduler/edge-nodes/status": update_edge_node_status,
            "/scheduler/cloud-nodes/status": update_cloud_node_status,
            "/scheduler/link-snapshots": update_link_snapshot,
            "/scheduler/tasks/result": save_task_result,
            "/scheduler/packet-route": route_packet,
            "/scheduler/cloud-upload-results": save_cloud_upload_result,
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
    default_runtime.start()
    try:
        print(f"scheduler service running at http://{selected_host}:{selected_port}")
        server.serve_forever()
    finally:
        close = getattr(server, "server_close", None)
        if callable(close):
            close()
        default_runtime.stop()


def _scheduler_config() -> Mapping[str, Any]:
    return load_config()["services"]["scheduler"]


if __name__ == "__main__":
    run()
