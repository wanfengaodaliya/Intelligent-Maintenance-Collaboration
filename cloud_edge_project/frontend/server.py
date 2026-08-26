# -*- coding: utf-8 -*-
"""前端网关服务器（纯本地开发用途）。

职责（单进程）：
1. 托管 frontend/ 目录下的静态页面；
2. 反向代理 /api/{edge01|edge02|scheduler|cloud|network}/... 到对应后端服务，
   规避浏览器跨域（后端未开启 CORS）；
3. 桥接 MQTT 实时消息（summary/device-results、summary/suggestions、edge/+/input），
   通过 SSE（Server-Sent Events）端点 /api/events 推送给浏览器。

依赖：Python 标准库 + paho-mqtt（项目 requirements-moment.txt 已包含）。

启动（在 cloud_edge_project/frontend 目录，需已激活 moment 环境）：
    python server.py            # 默认 http://127.0.0.1:8088
    python server.py --port 8099
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashboard_state import BinaryAccuracyEvaluator, DashboardSession

FRONTEND_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = FRONTEND_ROOT.parent

# 后端服务映射（与 start_project.ps1 / README.md 的端口约定一致）
BACKENDS: dict[str, str] = {
    "edge01": "http://127.0.0.1:8001",
    "edge02": "http://127.0.0.1:8002",
    "scheduler": "http://127.0.0.1:8003",
    "cloud": "http://127.0.0.1:8004",
    "network": "http://127.0.0.1:8090",
}

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPICS = ["summary/device-results", "summary/suggestions", "edge/+/input"]
PROXY_TIMEOUT_SECONDS = 60

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def classify_topic(topic: str) -> str:
    """把 MQTT topic 映射为 SSE 事件名。"""
    if topic == "summary/device-results":
        return "device-result"
    if topic == "summary/suggestions":
        return "suggestion"
    if topic.startswith("edge/") and topic.endswith("/input"):
        return "input-packet"
    return "other"


class MqttBridge:
    """后台 MQTT 订阅者：把消息广播给所有 SSE 客户端队列。"""

    def __init__(self, host: str, port: int, dashboard: DashboardSession) -> None:
        self._host = host
        self._port = port
        self._dashboard = dashboard
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict]] = []
        self.connected = False
        self._client = None
        threading.Thread(target=self._run, name="mqtt-bridge", daemon=True).start()

    def subscribe(self) -> tuple[queue.Queue[dict], dict]:
        q: queue.Queue[dict] = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
            snapshot = self._dashboard.snapshot()
        return q, snapshot

    def unsubscribe(self, q: queue.Queue[dict]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        with self._lock:
            packet_disposition = self._dashboard.record(event)
            outbound_event = (
                {**event, "packet_disposition": packet_disposition}
                if packet_disposition
                else event
            )
            for q in self._subscribers:
                try:
                    q.put_nowait(outbound_event)
                except queue.Full:
                    # 慢客户端直接恢复到当前权威快照，避免累计 KPI 永久少算。
                    while True:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    q.put_nowait(
                        {
                            "type": "session-snapshot",
                            "payload": self._dashboard.snapshot(),
                            "ts": time.time(),
                        }
                    )

    def _run(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("[mqtt-bridge] paho-mqtt 未安装，实时消息不可用（仅 HTTP 轮询）")
            return
        while True:
            try:
                self._client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id="frontend-bridge-%d" % int(time.time()),
                )
                self._client.on_connect = self._on_connect
                self._client.on_message = self._on_message
                self._client.on_disconnect = self._on_disconnect
                self._client.connect(self._host, self._port, keepalive=30)
                self._client.loop_forever(retry_first_connection=True)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                print("[mqtt-bridge] 连接失败: %s，5 秒后重试" % exc)
                self._broadcast({"type": "mqtt-status", "connected": False})
                time.sleep(5)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = True
        for topic in MQTT_TOPICS:
            client.subscribe(topic)
        self._broadcast({"type": "mqtt-status", "connected": True})
        print("[mqtt-bridge] 已连接 MQTT %s:%s，订阅 %s" % (self._host, self._port, MQTT_TOPICS))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        self._broadcast({"type": "mqtt-status", "connected": False})

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = msg.payload.decode("utf-8", errors="replace")
        self._broadcast(
            {
                "type": classify_topic(msg.topic),
                "topic": msg.topic,
                "payload": payload,
                "ts": time.time(),
            }
        )


DASHBOARD = DashboardSession()
ACCURACY = BinaryAccuracyEvaluator(
    PROJECT_ROOT / "sender_module" / "runtime" / "state" / "packet_source_mapping.db",
    PROJECT_ROOT / "data" / "cloud_review.db",
    started_at_ns=DASHBOARD.started_at_ns,
)
BRIDGE = MqttBridge(MQTT_HOST, MQTT_PORT, DASHBOARD)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FrontendGateway/1.0"

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # noqa: A003 - 覆写父类日志
        pass  # 静态资源访问不刷屏

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def do_OPTIONS(self):  # noqa: N802 - http.server 命名约定
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/events":
            self._handle_sse()
        elif path == "/api/dashboard/accuracy":
            self._send_json(200, ACCURACY.evaluate())
        elif path.startswith("/api/"):
            self._handle_proxy()
        else:
            self._serve_static(path)

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/"):
            self._handle_proxy()
        else:
            self._send_json(404, {"error": "not found"})

    do_PUT = do_POST  # noqa: N815
    do_DELETE = do_POST  # noqa: N815

    # ---------- SSE 实时流 ----------

    def _handle_sse(self) -> None:
        q, snapshot = BRIDGE.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(
                b"event: mqtt-status\ndata: {\"connected\": %s}\n\n"
                % (b"true" if BRIDGE.connected else b"false")
            )
            snapshot_event = {
                "type": "session-snapshot",
                "payload": snapshot,
                "ts": time.time(),
            }
            self.wfile.write(
                (
                    "event: session-snapshot\ndata: %s\n\n"
                    % json.dumps(snapshot_event, ensure_ascii=False)
                ).encode("utf-8")
            )
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    data = json.dumps(event, ensure_ascii=False, default=str)
                    self.wfile.write(
                        ("event: %s\ndata: %s\n\n" % (event["type"], data)).encode("utf-8")
                    )
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")  # 保活注释行
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 浏览器关闭页面，正常退出
        finally:
            BRIDGE.unsubscribe(q)

    # ---------- API 代理 ----------

    def _handle_proxy(self) -> None:
        parts = self.path.split("?", 1)
        segments = parts[0].split("/")  # ['', 'api', backend, rest...]
        if len(segments) < 3 or segments[1] != "api" or segments[2] not in BACKENDS:
            self._send_json(404, {"error": "unknown backend"})
            return
        base = BACKENDS[segments[2]]
        target = base + "/" + "/".join(segments[3:])
        if len(parts) > 1 and parts[1]:
            target += "?" + parts[1]

        body = None
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            body = self.rfile.read(length)

        request = urllib.request.Request(target, data=body, method=self.command)
        if body is not None:
            request.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        try:
            with urllib.request.urlopen(request, timeout=PROXY_TIMEOUT_SECONDS) as resp:
                self._proxy_response(resp.status, resp.read(), resp.headers.get("Content-Type"))
        except urllib.error.HTTPError as error:
            self._proxy_response(error.code, error.read(), error.headers.get("Content-Type"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            self._send_json(
                502,
                {
                    "error": "backend_unreachable",
                    "backend": segments[2],
                    "message": str(error),
                },
            )

    def _proxy_response(self, status: int, body: bytes, content_type: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ---------- 静态文件 ----------

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        file_path = (FRONTEND_ROOT / path.lstrip("/")).resolve()
        try:
            file_path.relative_to(FRONTEND_ROOT)
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        if not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content = file_path.read_bytes()
        suffix = file_path.suffix.lower()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="智能运维协作平台 前端网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print("=" * 60)
    print("  智能运维协作平台 前端已启动")
    print("  地址:   http://%s:%d" % (args.host, args.port))
    print("  代理:   edge01->8001  edge02->8002  scheduler->8003")
    print("          cloud->8004   network->8090")
    print("  实时流: MQTT %s:%s -> /api/events (SSE)" % (MQTT_HOST, MQTT_PORT))
    print("  退出:   Ctrl+C")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
