#这个文件负责把调度器包装成 HTTP 服务
"""HTTP API for the minimal PER-DDPG-style scheduler.

Primary endpoints:
    POST /scheduler/decide
    GET /health

The module exposes a FastAPI ``app`` when FastAPI is installed. It also includes
a small stdlib fallback server so the scheduler can run without extra packages:

    python api.py
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .rule_scheduler import PreDDPGScheduler
except ImportError:  # Allows running this file directly: python api.py
    from rule_scheduler import PreDDPGScheduler

try:
    from fastapi import APIRouter, FastAPI
except ImportError:
    APIRouter = None
    FastAPI = None


scheduler = PreDDPGScheduler()


#把 HTTP 请求体传给调度器，然后返回字典
def decide(request: dict[str, Any]) -> dict[str, Any]:
    """Return a ScheduleDecision dict for the documented request shape."""

    return scheduler.decide(request).to_dict()


#返回健康检查信息
def health() -> dict[str, Any]:
    return {
        "service": "scheduler_service",
        "node_id": "scheduler_1",
        "status": "ok",
        "model_loaded": True,
        "model_backend": "rule",    #当前不是训练模型，而是规则调度器
        "device": "cpu",
        "port": 8003,
    }


if APIRouter is not None:
    router = APIRouter(prefix="/scheduler", tags=["scheduler"])

    @router.post("/decide")
    def decide_endpoint(request: dict[str, Any]) -> dict[str, Any]:
        return decide(request)

    app = FastAPI(title="Minimal PER-DDPG Rule Scheduler")
    app.include_router(router)

    @app.get("/health")
    def health_endpoint() -> dict[str, Any]:
        return health()
else:
    router = None
    app = None


#为了最小可运行，我还加了标准库 HTTP 服务
class SchedulerRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, health())
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path != "/scheduler/decide":
            self._send_json(404, {"detail": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            request = json.loads(body) if body else {}
            self._send_json(200, decide(request))
        except json.JSONDecodeError:
            self._send_json(400, {"detail": "invalid JSON body"})
        except Exception as exc:
            self._send_json(500, {"detail": str(exc)})

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
