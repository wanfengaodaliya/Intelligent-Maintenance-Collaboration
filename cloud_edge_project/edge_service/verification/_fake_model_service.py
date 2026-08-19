# -*- coding: utf-8 -*-
"""可控故障的 Fake 模型服务（阶段 7.5 故障演练基础设施）。

用 stdlib HTTP 服务模拟正式模型服务的全部故障形态，供自动化矩阵测试与
双节点部署演练复用。真实 GPU 模型的冷启动耗时、显存稳定性等属于真实
环境验收项，不在本设施范围内（方案第 10 节：合成演练与业务准确率验收
必须明确区分）。

支持的模式（/infer 行为）：
  ok             返回合法 3 字段结果（含 request_id 回填）
  busy           立即返回 503 + MODEL_BUSY
  timeout        休眠 3s（超过客户端推理预算 → 推理超时）
  bad_json       返回 200 + 非法 JSON 体
  output_invalid 返回 MODEL_OUTPUT_INVALID（模型输出合同非法）
  input_invalid  返回 MODEL_INPUT_INVALID（输入字段越界）
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModelService:
    def __init__(self, version: str = "official-fake-v1", ready: bool = True) -> None:
        self.version = version
        self.ready = ready
        self.mode = "ok"
        self.infer_requests = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.port: int | None = None

    # ---- 生命周期 ----

    def start(self, port: int = 0) -> "FakeModelService":
        service = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # 静音访问日志
                pass

            def _send(self, code: int, payload: dict | str) -> None:
                body = payload if isinstance(payload, str) else json.dumps(payload)
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/health":
                    self._send(200, {"status": "ok"})
                elif self.path.rstrip("/") == "/readiness":
                    with service._lock:
                        self._send(200, {
                            "ready": service.ready,
                            "load_error": None,
                            "model_version": service.version,
                        })
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path.rstrip("/") != "/infer":
                    self._send(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                request_id = body.get("request_id")
                with service._lock:
                    service.infer_requests += 1
                    mode = service.mode
                    version = service.version
                if mode == "timeout":
                    time.sleep(3.0)
                    return
                if mode == "bad_json":
                    self._send(200, "not-json{corrupted")
                    return
                if mode == "busy":
                    self._send(503, {"valid": False, "error": "MODEL_BUSY",
                                     "request_id": request_id})
                    return
                if mode == "output_invalid":
                    self._send(502, {"valid": False, "error": "MODEL_OUTPUT_INVALID",
                                     "request_id": request_id})
                    return
                if mode == "input_invalid":
                    self._send(400, {"valid": False, "error": "MODEL_INPUT_INVALID",
                                     "detail": "confidence out of range",
                                     "request_id": request_id})
                    return
                self._send(200, {
                    "valid": True,
                    "edge_result": "normal",
                    "edge_risk_level": "low",
                    "confidence": 0.92,
                    "model_version": version,
                    "request_id": request_id,
                    "raw_text": '{"edge_result":"normal","edge_risk_level":"low","confidence":0.92}',
                    "latency_ms": 5.0,
                })

        server = ThreadingHTTPServer(("0.0.0.0" if port else "127.0.0.1", port), _Handler)
        server.daemon_threads = True  # timeout 模式的休眠线程不阻塞 shutdown
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="fake-model-service").start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self.port = None

    # ---- 演练控制 ----

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode

    def set_version(self, version: str) -> None:
        with self._lock:
            self.version = version

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self.ready = ready

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("fake model service is not running")
        return "http://127.0.0.1:%d" % self.port


if __name__ == "__main__":
    # 部署演练入口：在宿主机 8012 起一个正常 fake 模型服务（Ctrl+C 退出）。
    service = FakeModelService().start(port=8012)
    print("fake model service on http://0.0.0.0:8012 (version=%s)" % service.version)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        service.stop()
