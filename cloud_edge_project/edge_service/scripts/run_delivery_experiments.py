# -*- coding: utf-8 -*-
"""阶段 7：端到端交付实验运行器（固定演示 + 故障矩阵 + 机器可读报告）。

前置条件（见 docs/交付与可复现实验指南.md）：
  ① 网络模拟器已启动（Toxiproxy/MQTT/network-controller）
  ② 宿主机 8012 端口空闲（运行器自带可控 Fake 模型服务）
  ③ 双 Edge 容器已启动（compose.network-sim.yml，端口 8001/8002）
  Scheduler/Cloud 可不启动（暂定结果路径正是无 Cloud 时的预期行为）。

固定演示场景：
  task_flow_baseline_provisional  双节点任务注册 → 80 包/轴承确定性数据 →
                                  Cloud 缺席下的暂定设备结果（含 trace 身份字段）
  model_unavailable_isolation     模型服务停止 → 双节点 readiness 503 隔离 → 恢复
  sender_link_fault_isolation     Sender→Edge MQTT 链路 down → 故障隔离不串扰 → 恢复
  edge_node_restart_isolation     节点容器停止 → 对端无影响 → 重启恢复
  duplicate_packet_stability      重复包幂等（无拒绝计数增长、节点稳定）

用法（在 cloud_edge_project 下）：
  python edge_service/scripts/run_delivery_experiments.py --json report.json
  python edge_service/scripts/run_delivery_experiments.py --only model_unavailable_isolation
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _path in (PROJECT_ROOT / "edge_service" / "src",
              PROJECT_ROOT / "edge_service" / "verification",
              PROJECT_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import paho.mqtt.client as mqtt  # noqa: E402

from _fake_model_service import FakeModelService  # noqa: E402
from health_check import run_checks  # noqa: E402

EDGE_01_BASE = "http://127.0.0.1:8001"
EDGE_02_BASE = "http://127.0.0.1:8002"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
DEVICE_RESULT_TOPIC = "summary/device-results"
TOXIPROXY = "http://127.0.0.1:8474"
# entities.yaml：sender_01 base 18831（edge_01/edge_02 递增），sender_02 base 18931。
SENDER_PORTS = {("sender_01", "edge_01"): 18831, ("sender_01", "edge_02"): 18832,
                ("sender_02", "edge_01"): 18931, ("sender_02", "edge_02"): 18932}
PACKETS_PER_BEARING = 80
COMPOSE_FILE = "edge_service/compose.network-sim.yml"
# 运行 nonce：任务 ID 唯一化，避免跨运行触发任务冲突（Edge 任务态为进程内存态）。
RUN_TOKEN = time.strftime("%Y%m%d%H%M%S")


# ---------- 工具 ----------


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _http(method: str, url: str, payload: dict | None = None,
          timeout_s: float = 5.0) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def _git_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, *args],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180)


# ---------- 固定演示数据（确定性，seed 内置） ----------


def build_packet(device_id: str, bearing_id: str, sender_id: str, task_id: str,
                 sequence: int) -> dict:
    """与正式 sensor 包合同一致的确定性合成包（无外部数据依赖）。"""
    vibration = [
        0.35 * math.sin(2.0 * math.pi * 1_000.0 * index / 64_000.0)
        for index in range(3_200)
    ]
    return {
        "device_id": device_id,
        "bearing_id": bearing_id,
        "task_id": task_id,
        "packet_id": "pkt_%s_%s_%03d" % (device_id, bearing_id, sequence),
        "sender_id": sender_id,
        "sequence_number": sequence,
        "end_generate_timestamp_ns": 50_000_000 + sequence * 50_000_000,
        "data": {
            "vibration": {"sample_rate_hz": 64_000, "sample_count": 3_200,
                          "values": vibration, "unit": "mm/s"},
            "phase_current_1_A": {"sample_rate_hz": 64_000, "sample_count": 3_200,
                                  "values": [1.0] * 3_200, "unit": "A"},
            "phase_current_2_A": {"sample_rate_hz": 64_000, "sample_count": 3_200,
                                  "values": [1.0] * 3_200, "unit": "A"},
            "shaft_speed_rpm": {"sample_rate_hz": 4_000, "sample_count": 200,
                                "values": [1_350.0] * 200, "unit": "rpm"},
            "load_torque_nm": {"sample_rate_hz": 4_000, "sample_count": 200,
                               "values": [1.1] * 200, "unit": "N·m"},
            "bearing_radial_load_n": {"sample_rate_hz": 4_000, "sample_count": 200,
                                      "values": [880.0] * 200, "unit": "N"},
            "bearing_module_temperature_c": 46.0,
        },
    }


def build_dispatch(task_id: str, edge_node_id: str, device_id: str,
                   bearing_senders: list[tuple[str, str]]) -> dict:
    return {
        "task_id": task_id,
        "dispatch_id": "dispatch_%s" % task_id,
        "target_edge_node_id": edge_node_id,
        "task_type": "BEARING_EDGE_INFERENCE",
        "dispatched_at_ns": time.time_ns(),
        "input_ref": {
            "device_id": device_id,
            "expected_bearing_ids": sorted(b for b, _ in bearing_senders),
            "assigned_bearings": [
                {"bearing_id": bearing, "sender_id": sender,
                 "expected_packet_count": PACKETS_PER_BEARING}
                for bearing, sender in bearing_senders
            ],
        },
    }


# ---------- 观测与注入 ----------


class DeviceResultCollector:
    """订阅设备结果主题，捕获带时间戳的发布。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="delivery-experiments-collector", protocol=mqtt.MQTTv311,
        )
        self.client.on_message = self._on_message

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"decode_error": True}
        with self._lock:
            self.messages.append({"received_at_ms": _now_ms(),
                                  "topic": message.topic, "payload": payload})

    def __enter__(self) -> "DeviceResultCollector":
        self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
        self.client.subscribe(DEVICE_RESULT_TOPIC, qos=1)
        self.client.loop_start()
        return self

    def __exit__(self, *exc) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def for_task(self, task_id: str) -> list[dict]:
        with self._lock:
            return [m for m in self.messages
                    if m["payload"].get("task_id") == task_id]


class MqttLinkPublisher:
    """经 Toxiproxy Sender 链路端口发布（复刻真实 Sender 数据面）。

    连接与发布均具备一次重试：长发送阶段链路偶发重置时自动恢复。
    """

    def __init__(self, sender_id: str, edge_node_id: str) -> None:
        self.port = SENDER_PORTS[(sender_id, edge_node_id)]
        self.topic = "edge/%s/input" % edge_node_id
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="delivery-exp-%s-%s-%d" % (sender_id, edge_node_id, time.monotonic_ns()),
            protocol=mqtt.MQTTv311,
        )

    def __enter__(self) -> "MqttLinkPublisher":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def connect(self, attempts: int = 3) -> None:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                self.client.connect(BROKER_HOST, self.port, keepalive=30)
                self.client.loop_start()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.8)
        raise ConnectionError("link :%d connect failed: %s" % (self.port, last_error))

    def publish(self, packet: dict, timeout_s: float = 8.0) -> tuple[bool, str]:
        for attempt in (1, 2):
            try:
                info = self.client.publish(self.topic, json.dumps(packet), qos=1)
                deadline = time.monotonic() + timeout_s
                while not info.is_published() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if info.is_published():
                    return True, ""
                last_err = "publish ack timeout via link :%d" % self.port
            except Exception as exc:  # noqa: BLE001
                last_err = "%s: %s" % (type(exc).__name__, exc)
            if attempt == 1:
                # 断线重连一次（链路被网络模拟器重置的场景）。
                try:
                    self.client.loop_stop()
                    self.connect()
                except Exception as exc:  # noqa: BLE001
                    return False, "reconnect failed: %s" % exc
        return False, last_err or "publish failed"


def link_set_enabled(proxy: str, enabled: bool) -> tuple[int, object]:
    """Toxiproxy v2 断连语义：禁用/启用代理本身（无 down toxic 类型）。"""
    status, body = _http("POST", "%s/proxies/%s" % (TOXIPROXY, proxy),
                         {"enabled": enabled})
    ok = status == 200 and (not isinstance(body, dict)
                            or body.get("enabled") is enabled)
    return (200 if ok else status), body


def edge_health(base: str) -> dict:
    _, body = _http("GET", base + "/health")
    return body if isinstance(body, dict) else {}


def wait_for(predicate, timeout_s: float, interval_s: float = 0.5,
             what: str = "condition") -> tuple[bool, float]:
    started = _now_ms()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True, _now_ms() - started
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval_s)
    return False, _now_ms() - started


# ---------- 场景 ----------


def scenario_task_flow_baseline(context: dict) -> dict:
    """双节点任务注册 + 确定性数据 + Cloud 缺席暂定结果。"""
    started = _now_ms()
    task_e1 = "task_sim_e1_%s" % RUN_TOKEN
    task_e2 = "task_sim_e2_%s" % RUN_TOKEN
    tasks = [
        (task_e1, "edge_01", EDGE_01_BASE, "device_sim_01",
         [("bearing_01", "sender_01"), ("bearing_02", "sender_02")]),
        (task_e2, "edge_02", EDGE_02_BASE, "device_sim_02",
         [("bearing_01", "sender_01"), ("bearing_02", "sender_02")]),
    ]
    acks = {}
    for task_id, edge_id, base, device_id, bearings in tasks:
        status, body = _http("POST", base + "/edge/tasks",
                             build_dispatch(task_id, edge_id, device_id, bearings))
        acks[edge_id] = {"http": status, "ack": body}
    assertions = {"task_ack_edge_01": acks["edge_01"]["http"] == 200
                  and acks["edge_01"]["ack"].get("ack_status") == "ACCEPTED",
                  "task_ack_edge_02": acks["edge_02"]["http"] == 200
                  and acks["edge_02"]["ack"].get("ack_status") == "ACCEPTED"}

    sent, publish_errors = 0, []
    send_started = _now_ms()
    with DeviceResultCollector() as collector, \
            MqttLinkPublisher("sender_01", "edge_01") as p1a, \
            MqttLinkPublisher("sender_02", "edge_01") as p2a, \
            MqttLinkPublisher("sender_01", "edge_02") as p1b, \
            MqttLinkPublisher("sender_02", "edge_02") as p2b:
        publishers = {("sender_01", "edge_01"): p1a, ("sender_02", "edge_01"): p2a,
                      ("sender_01", "edge_02"): p1b, ("sender_02", "edge_02"): p2b}
        for sequence in range(1, PACKETS_PER_BEARING + 1):
            for task_id, edge_id, base, device_id, bearings in tasks:
                for bearing, sender in bearings:
                    ok, err = publishers[(sender, edge_id)].publish(
                        build_packet(device_id, bearing, sender, task_id, sequence))
                    sent += 1
                    if not ok:
                        publish_errors.append(err)
            time.sleep(0.01)  # 加速节奏（演示允许，正式 Sender 为 50ms）
        send_elapsed = _now_ms() - send_started

        # 等待双节点暂定设备结果（round 超时 3.5s + 发布重试预算）。
        def _both_published() -> bool:
            return len(collector.for_task(task_e1)) >= 1 \
                and len(collector.for_task(task_e2)) >= 1
        got, wait_elapsed = wait_for(_both_published, timeout_s=60.0,
                                     interval_s=1.0, what="provisional device results")
        results_e1 = collector.for_task(task_e1)
        results_e2 = collector.for_task(task_e2)

    def _trace_ok(results: list[dict]) -> bool:
        return bool(results) and all(
            all(k in r["payload"] for k in
                ("trace_id", "task_id", "edge_node_id", "decision_round_id"))
            for r in results)

    assertions.update({
        "all_packets_published": not publish_errors,
        "provisional_result_edge_01": len(results_e1) >= 1,
        "provisional_result_edge_02": len(results_e2) >= 1,
        "trace_identity_present": _trace_ok(results_e1) and _trace_ok(results_e2),
        "edges_remain_live": all(edge_health(b).get("liveness", {}).get("alive")
                                 is True for b in (EDGE_01_BASE, EDGE_02_BASE)),
    })
    h1 = edge_health(EDGE_01_BASE)
    return {
        "started_at_epoch_ms": started, "packets_sent": sent,
        "publish_errors": publish_errors[:5],
        "send_elapsed_ms": round(send_elapsed), "result_wait_ms": round(wait_elapsed),
        "task_acks": {k: {"http": v["http"], "ack_status":
                          (v["ack"] or {}).get("ack_status") if isinstance(v["ack"], dict) else None}
                      for k, v in acks.items()},
        "device_results": {
            "edge_01": [{"decision_source": r["payload"].get("decision_source"),
                         "action_grade": r["payload"].get("action_grade"),
                         "trace_id": r["payload"].get("trace_id")} for r in results_e1],
            "edge_02": [{"decision_source": r["payload"].get("decision_source"),
                         "action_grade": r["payload"].get("action_grade"),
                         "trace_id": r["payload"].get("trace_id")} for r in results_e2],
        },
        "mqtt_capacity_edge_01": h1.get("mqtt_capacity"),
        "assertions": assertions,
    }


def scenario_model_unavailable_isolation(context: dict) -> dict:
    fake: FakeModelService = context["fake"]
    bases = (EDGE_01_BASE, EDGE_02_BASE)

    def _ready_all() -> bool:
        return all(edge_health(b).get("readiness", {}).get("ready") is True for b in bases)

    def _isolated_all() -> bool:
        return all(edge_health(b).get("readiness", {}).get(
            "checks", {}).get("model_service_ready") is False for b in bases)

    def _live_all() -> bool:
        return all(edge_health(b).get("liveness", {}).get("alive") is True for b in bases)

    fake.stop()
    down_ok, down_ms = wait_for(_isolated_all, timeout_s=15.0, what="model isolation")
    live_during = all(_safe_live(b) for b in bases)
    fake.start(port=8012)
    up_ok, up_ms = wait_for(_ready_all, timeout_s=15.0, what="model recovery")
    return {
        "assertions": {
            "isolation_within_15s": down_ok,
            "liveness_unaffected": live_during,
            "recovery_within_15s": up_ok,
        },
        "isolate_detected_ms": round(down_ms), "recovery_detected_ms": round(up_ms),
    }


def _safe_live(base: str) -> bool:
    try:
        return edge_health(base).get("liveness", {}).get("alive") is True
    except Exception:  # noqa: BLE001
        return False


def scenario_sender_link_fault_isolation(context: dict) -> dict:
    proxy = "sender_01__to__edge_01__mqtt"
    task_link = "task_link_fault_%s" % RUN_TOKEN
    status_down, _ = link_set_enabled(proxy, enabled=False)
    packet_bad = build_packet("device_link", "bearing_01", "sender_01",
                              task_link, 1)
    ok_bad, err_bad = False, "connect failed"
    try:
        with MqttLinkPublisher("sender_01", "edge_01") as broken:
            ok_bad, err_bad = broken.publish(packet_bad, timeout_s=3.0)
    except ConnectionError as exc:
        err_bad = str(exc)  # 链路禁用：连接被拒/超时即预期阻断形态
    with MqttLinkPublisher("sender_02", "edge_01") as healthy:
        packet_ok = build_packet("device_link", "bearing_02", "sender_02",
                                 task_link, 1)
        ok_good, err_good = healthy.publish(packet_ok, timeout_s=3.0)
    status_up, _ = link_set_enabled(proxy, enabled=True)
    time.sleep(0.5)
    ok_fix = False
    try:
        with MqttLinkPublisher("sender_01", "edge_01") as restored:
            ok_fix, _ = restored.publish(
                build_packet("device_link", "bearing_01", "sender_01",
                             task_link, 2), timeout_s=3.0)
    except ConnectionError:
        ok_fix = False
    return {
        "assertions": {
            "link_disabled": status_down == 200,
            "faulted_link_blocked": not ok_bad,
            "peer_link_unaffected": ok_good,
            "fault_cleared_after_reenable": ok_fix,
            "link_reenabled": status_up == 200,
        },
        "faulted_link_error": err_bad[:120],
    }


def scenario_edge_node_restart_isolation(context: dict) -> dict:
    stop = _compose("stop", "edge_02")
    down_ok, _ = wait_for(
        lambda: _http("GET", EDGE_02_BASE + "/health/live")[0] == 0,
        timeout_s=30.0, what="edge_02 down")
    peer_live = _safe_live(EDGE_01_BASE)
    start = _compose("start", "edge_02")

    def _edge02_ready() -> bool:
        return edge_health(EDGE_02_BASE).get("readiness", {}).get("ready") is True
    up_ok, up_ms = wait_for(_edge02_ready, timeout_s=90.0, interval_s=2.0,
                            what="edge_02 recovery")
    return {
        "assertions": {
            "stop_succeeded": stop.returncode == 0,
            "edge_02_down_detected": down_ok,
            "edge_01_unaffected": peer_live,
            "start_succeeded": start.returncode == 0,
            "edge_02_ready_again": up_ok,
        },
        "recovery_detected_ms": round(up_ms),
    }


def scenario_duplicate_packet_stability(context: dict) -> dict:
    base = EDGE_01_BASE
    task_id = "task_dup_check_%s" % RUN_TOKEN
    status, ack = _http("POST", base + "/edge/tasks", build_dispatch(
        task_id, "edge_01", "device_dup",
        [("bearing_01", "sender_01"), ("bearing_02", "sender_02")]))
    rejected_before = edge_health(base).get("mqtt_capacity", {}).get("rejected_total")
    with MqttLinkPublisher("sender_01", "edge_01") as publisher:
        packet = build_packet("device_dup", "bearing_01", "sender_01", task_id, 1)
        ok_first, _ = publisher.publish(packet)
        ok_dupe, _ = publisher.publish(packet)  # 完全相同的 packet_id/sequence
        time.sleep(1.0)
    health = edge_health(base)
    rejected_after = health.get("mqtt_capacity", {}).get("rejected_total")
    return {
        "assertions": {
            "task_accepted": status == 200,
            "first_publish_ok": ok_first,
            "duplicate_publish_returned": ok_dupe,  # MQTT 层 QoS1 确认（应用层去重）
            "no_capacity_rejects": rejected_before == rejected_after,
            "edge_still_live": health.get("liveness", {}).get("alive") is True,
        },
        "rejected_before": rejected_before, "rejected_after": rejected_after,
    }


SCENARIOS = {
    "task_flow_baseline_provisional": scenario_task_flow_baseline,
    "model_unavailable_isolation": scenario_model_unavailable_isolation,
    "sender_link_fault_isolation": scenario_sender_link_fault_isolation,
    "edge_node_restart_isolation": scenario_edge_node_restart_isolation,
    "duplicate_packet_stability": scenario_duplicate_packet_stability,
}


# ---------- 主流程 ----------


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 7 端到端交付实验")
    parser.add_argument("--json", type=str, default=None, help="机器可读报告输出路径")
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS),
                        help="只运行指定场景（可重复）")
    args = parser.parse_args()

    preflight = run_checks(strict=False)
    required_ok = preflight["summary"]["failed_required"] == 0
    if not required_ok:
        print("preflight FAILED: 必需组件未就绪，先按交付指南启动环境")
        print("  " + ", ".join(c["name"] for c in preflight["checks"]
                               if c["required"] and c["status"] == "fail"))

    report: dict = {
        "schema": "edge-delivery-experiments/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "environment": {
            "model_service": "FakeModelService(official-fake-v1)",
            "cloud": "absent (provisional path expected)",
            "scheduler": "absent (task registered directly)",
            "packets_per_bearing": PACKETS_PER_BEARING,
        },
        "preflight_healthy": required_ok,
        "scenarios": {},
    }

    context: dict = {}
    selected = args.only or list(SCENARIOS)
    if required_ok:
        fake = FakeModelService(version="official-fake-v1").start(port=8012)
        context["fake"] = fake
        # 等双节点感知模型就绪。
        wait_for(lambda: all(edge_health(b).get("readiness", {}).get("ready") is True
                             for b in (EDGE_01_BASE, EDGE_02_BASE)),
                 timeout_s=15.0, what="initial readiness")
    try:
        for name in selected:
            print("== scenario: %s" % name, flush=True)
            started = _now_ms()
            try:
                result = SCENARIOS[name](context)
                error = None
            except Exception as exc:  # noqa: BLE001
                result, error = {}, "%s: %s" % (type(exc).__name__, exc)
            entry = {"elapsed_ms": round(_now_ms() - started),
                     "pass": bool(result.get("assertions")
                                  and all(result["assertions"].values())
                                  and error is None),
                     **result}
            if error:
                entry["error"] = error
            report["scenarios"][name] = entry
            print("   pass=%s elapsed=%dms" % (entry["pass"], entry["elapsed_ms"]),
                  flush=True)
    finally:
        fake = context.get("fake")
        if fake is not None:
            fake.stop()

    report["summary"] = {
        "total": len(report["scenarios"]),
        "passed": sum(1 for s in report["scenarios"].values() if s["pass"]),
    }
    report["all_passed"] = required_ok and \
        report["summary"]["passed"] == report["summary"]["total"]

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print("report written: %s" % path)

    for name, entry in report["scenarios"].items():
        failed = [k for k, v in (entry.get("assertions") or {}).items() if not v]
        print("  [%s] %s%s" % ("PASS" if entry["pass"] else "FAIL", name,
                               (" — failed: " + ",".join(failed)) if failed else ""))
    print("experiments all_passed: %s" % report["all_passed"])
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
