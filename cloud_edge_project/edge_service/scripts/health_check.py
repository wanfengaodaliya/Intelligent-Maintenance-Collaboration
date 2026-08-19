# -*- coding: utf-8 -*-
"""阶段 7：一键健康检查（交付验收入口）。

探测全链路组件并输出人类可读摘要 + 机器可读 JSON 报告：

  必需（网络模拟编排 + 双 Edge）：
    mqtt-broker / toxiproxy / network-controller / edge_01 / edge_02
  可选（宿主机服务，演示完整链路时必需）：
    model_service / scheduler / cloud

用法（在 cloud_edge_project 下）：
  python edge_service/scripts/health_check.py              # 摘要输出，失败退出码 1
  python edge_service/scripts/health_check.py --json out.json
  python edge_service/scripts/health_check.py --strict     # 可选组件失败也返回 1

报告记录时间戳、git 版本、模型版本 pin 与各组件探测详情，
满足方案第 8 节"机器可读结果 + 环境/版本信息"要求。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HTTP_TIMEOUT_S = 3.0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _probe_targets() -> list[dict]:
    model_base = _env("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012").rstrip("/")
    return [
        {"name": "mqtt_broker", "kind": "tcp", "host": _env("EDGE_MQTT_HOST", "127.0.0.1"),
         "port": int(_env("EDGE_MQTT_PORT", "1883")), "required": True},
        {"name": "toxiproxy", "kind": "http", "url": _env("TOXIPROXY_API_URL", "http://127.0.0.1:8474/proxies"),
         "required": True},
        {"name": "network_controller", "kind": "http", "url": _env("EDGE_NETWORK_CONTROLLER_URL", "http://127.0.0.1:8090/health"),
         "required": True},
        {"name": "edge_01", "kind": "edge", "base": _env("EDGE_01_BASE_URL", "http://127.0.0.1:8001"),
         "required": True},
        {"name": "edge_02", "kind": "edge", "base": _env("EDGE_02_BASE_URL", "http://127.0.0.1:8002"),
         "required": True},
        {"name": "model_service", "kind": "model", "base": model_base, "required": False},
        {"name": "scheduler", "kind": "http", "url": _env("SCHEDULER_HEALTH_URL", "http://127.0.0.1:8003/health"),
         "required": False},
        {"name": "cloud", "kind": "http", "url": _env("CLOUD_HEALTH_URL", "http://127.0.0.1:8004/health"),
         "required": False},
    ]


def _fetch_json(url: str) -> tuple[bool, object, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return True, json.loads(body), ""
            except json.JSONDecodeError as exc:
                return False, body[:200], "invalid json: %s" % exc
    except urllib.error.HTTPError as exc:
        return False, None, "http %d" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, None, "%s: %s" % (type(exc).__name__, exc)


def _probe_tcp(host: str, port: int) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=HTTP_TIMEOUT_S):
            return True, "connect ok in %dms" % int((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _digest(body: object, *keys: str) -> dict:
    if not isinstance(body, dict):
        return {key: None for key in keys}
    return {key: body.get(key) for key in keys}


def _probe_edge(base: str) -> dict:
    """Edge 节点：liveness 必须通过；readiness/model_service 进详情。"""
    live_ok, live_body, live_err = _fetch_json(base + "/health/live")
    ready_ok, ready_body, ready_err = _fetch_json(base + "/health/ready")
    _, full_body, _ = _fetch_json(base + "/health")
    detail = {
        "live": {"ok": live_ok, "error": live_err, "body": live_body},
        "ready": {"ok": ready_ok, "error": ready_err, "body": ready_body},
        **_digest(full_body, "node_id", "model_backend", "model_version",
                  "model_service", "outbound_routes"),
    }
    return {"ok": bool(live_ok), "detail": detail,
            "summary": "live=%s ready=%s" % (live_ok, ready_ok)}


def _probe_model(base: str) -> dict:
    health_ok, health_body, health_err = _fetch_json(base + "/health")
    ready_ok, ready_body, ready_err = _fetch_json(base + "/readiness")
    version = ready_body.get("model_version") if isinstance(ready_body, dict) else None
    load_error = ready_body.get("load_error") if isinstance(ready_body, dict) else None
    return {"ok": bool(health_ok and ready_ok),
            "detail": {"health": {"ok": health_ok, "error": health_err},
                       "readiness": {"ok": ready_ok, "error": ready_err,
                                     "model_version": version, "load_error": load_error}},
            "summary": "ready=%s version=%s" % (ready_ok, version)}


def run_checks(strict: bool = False) -> dict:
    checks: list[dict] = []
    for target in _probe_targets():
        started = time.monotonic()
        if target["kind"] == "tcp":
            ok, info = _probe_tcp(target["host"], target["port"])
            detail, summary = {"error": None if ok else info}, info
        elif target["kind"] == "edge":
            result = _probe_edge(target["base"])
            ok, detail, summary = result["ok"], result["detail"], result["summary"]
        elif target["kind"] == "model":
            result = _probe_model(target["base"])
            ok, detail, summary = result["ok"], result["detail"], result["summary"]
        else:
            ok, body, err = _fetch_json(target["url"])
            detail, summary = {"error": err, "body": body}, ("ok" if ok else err)
        checks.append({
            "name": target["name"], "required": target["required"],
            "status": "pass" if ok else "fail",
            "summary": summary, "detail": detail,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        })
    failed_required = [c for c in checks if c["required"] and c["status"] == "fail"]
    failed_optional = [c for c in checks if not c["required"] and c["status"] == "fail"]
    healthy = not failed_required and (not strict or not failed_optional)
    return {
        "schema": "edge-delivery-health-check/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "environment": {
            "edge_model_base_url": _env("EDGE_MODEL_BASE_URL", "http://127.0.0.1:8012"),
            "edge_model_version_pin": os.environ.get("EDGE_MODEL_VERSION") or "(unpinned)",
            "model_edge_backend": "official",
        },
        "healthy": healthy,
        "strict": strict,
        "summary": {"total": len(checks), "passed": sum(c["status"] == "pass" for c in checks),
                    "failed_required": len(failed_required), "failed_optional": len(failed_optional)},
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="一键健康检查（交付验收）")
    parser.add_argument("--json", type=str, default=None, help="机器可读报告输出路径")
    parser.add_argument("--strict", action="store_true", help="可选组件失败也视为不健康")
    args = parser.parse_args()

    report = run_checks(strict=args.strict)

    print("== 交付健康检查 %s ==" % report["generated_at"][:19])
    print("git=%s  model_pin=%s" % (report["git_commit"],
                                    report["environment"]["edge_model_version_pin"]))
    for check in report["checks"]:
        marker = "PASS" if check["status"] == "pass" else "FAIL"
        tag = "" if check["required"] else " (optional)"
        print("  [%s] %-18s%s %s — %s" % (marker, check["name"], tag,
                                          check["summary"], check["detail"].get("error") or ""))
    print("summary: %(passed)d/%(total)d passed, required_fail=%(failed_required)d, "
          "optional_fail=%(failed_optional)d" % report["summary"])
    print("healthy: %s" % report["healthy"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("report written: %s" % args.json)
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
