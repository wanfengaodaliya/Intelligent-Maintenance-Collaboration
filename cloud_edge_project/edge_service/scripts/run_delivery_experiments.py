# -*- coding: utf-8 -*-
"""阶段 7：端到端交付实验运行器（固定演示 + 故障矩阵 + 机器可读报告）。

前置条件（见 docs/交付与可复现实验指南.md）：
  ① 网络模拟器已启动（Toxiproxy/MQTT/network-controller）
  ② 宿主机 8012 端口空闲（运行器自带可控 Fake 模型服务）
  ③ 双 Edge 容器已启动（compose.network-sim.yml，端口 8001/8002）
  Cloud 可不启动（暂定结果路径正是无 Cloud 时的预期行为）；
  Scheduler 必须启动（决策流依赖 /scheduler/packet-route 路由，指南第 ③ 步）。

固定演示场景：
  task_flow_baseline_provisional  经 Scheduler /decide 派发双任务（真实调度链路）→
                                  80 包/轴承确定性数据 → Cloud 缺席下的
                                  暂定设备结果（含 trace 身份字段）
  model_unavailable_isolation     模型服务停止 → 双节点 readiness 503 隔离 → 恢复
  sender_link_fault_isolation     Sender→Edge MQTT 链路 down → 故障隔离不串扰 → 恢复
  edge_node_restart_isolation     节点容器停止 → 对端无影响 → 重启恢复
  duplicate_packet_stability      重复包幂等（无拒绝计数增长、节点稳定）
  model_queue_saturation          模型推理挂起 + 80 包突发 → 队列满载拒绝可观测
                                  （/health model_queue）→ 恢复后排空，节点存活

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
# 本脚本的故障演练矩阵（model_unavailable / queue_saturation 等）依赖可注入
# 故障的 Fake 模型服务（8012），因此脚本启动的 Edge 容器固定 official 后端
# （覆盖 compose 的 local_h5 默认）。H5 三通道正式路线的验证见
# verification/test_local_h5_route.py 与 compose 手工演练。
os.environ["EDGE_DIAGNOSTIC_BACKEND"] = "official"


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
    """经 Scheduler 派发双任务 + 确定性数据 + Cloud 缺席暂定结果。

    真实链路：Sender 请求 /scheduler/decide（按轴承，task_id 合同
    sd_<sender>_tk_<0001-9999>）→ Scheduler 选节点并推送 Edge 注册 →
    Sender 经 Toxiproxy 链路发 80 包/轴承 → Cloud 缺席下 Edge 在业务
    截止时间降级发布暂定设备结果。
    """
    started = _now_ms()
    # 决策流依赖 Scheduler 路由（packet-route）；缺席时本场景必失败，先行明示。
    scheduler_status, _ = _http(
        "GET", os.getenv("SCHEDULER_HEALTH_URL", "http://127.0.0.1:8003/health"))
    scheduler_base = os.getenv(
        "SCHEDULER_SERVICE_URL", "http://127.0.0.1:8003").rstrip("/")
    # 任务号取 RUN_TOKEN 的 HHMMSS（%10000），跨运行基本不重复且符合合同。
    task_base = int(RUN_TOKEN[-6:]) % 10000 or 1
    specs = [
        {"device_id": "device_sim_01", "sender_id": "sender_01",
         "task_id": "sd_01_tk_%04d" % (task_base % 9999 or 1),
         "bearing_id": "bearing_01"},
        {"device_id": "device_sim_02", "sender_id": "sender_02",
         "task_id": "sd_02_tk_%04d" % ((task_base + 1) % 9999 or 1),
         "bearing_id": "bearing_01"},
    ]
    decisions = {}
    for spec in specs:
        payload = {
            **spec,
            "packet_size_bytes": 64_000,
            "expected_packet_count": PACKETS_PER_BEARING,
            "expected_duration_ms": 4_000,
            "created_timestamp_ns": time.time_ns(),
        }
        # Scheduler 侧节点视图依赖周期性状态上报（模型加载/资源），就绪存在
        # 滞后；503 NO_AVAILABLE_EDGE_NODE 时按节奏重试至状态刷新。
        for attempt in range(20):
            status, body = _http("POST", scheduler_base + "/scheduler/decide", payload)
            if status == 200 or attempt == 19:
                break
            time.sleep(1.5)
        decisions[spec["task_id"]] = {"http": status, "decision": body}
    assertions = {"scheduler_available": scheduler_status == 200}
    for task_id, entry in decisions.items():
        ok = entry["http"] == 200 and isinstance(entry["decision"], dict) \
            and entry["decision"].get("target_topic")
        assertions["decide_ack_%s" % task_id] = ok

    sent, publish_errors = 0, []
    send_started = _now_ms()
    send_elapsed, wait_elapsed, results = 0.0, 0.0, {s["task_id"]: [] for s in specs}
    decide_ok = all(a for k, a in assertions.items() if k.startswith("decide_ack"))
    collector: DeviceResultCollector | None = None
    publishers: dict[tuple[str, str], MqttLinkPublisher] = {}
    if decide_ok:
        collector = DeviceResultCollector().__enter__()
        # 按派发结论动态建链：target_topic 形如 edge/edge_XX/input。
        publishers: dict[tuple[str, str], MqttLinkPublisher] = {}
        try:
            for spec in specs:
                decision = decisions[spec["task_id"]]["decision"]
                edge_id = str(decision.get("target_topic", "")).split("/")[1]
                key = (spec["sender_id"], edge_id)
                if key not in publishers:
                    publishers[key] = MqttLinkPublisher(*key).__enter__()
            for sequence in range(1, PACKETS_PER_BEARING + 1):
                for spec in specs:
                    decision = decisions[spec["task_id"]]["decision"]
                    edge_id = str(decision.get("target_topic", "")).split("/")[1]
                    publisher = publishers[(spec["sender_id"], edge_id)]
                    ok, err = publisher.publish(build_packet(
                        spec["device_id"], spec["bearing_id"], spec["sender_id"],
                        spec["task_id"], sequence))
                    sent += 1
                    if not ok:
                        publish_errors.append(err)
                time.sleep(0.01)  # 加速节奏（演示允许，正式 Sender 为 50ms）
            send_elapsed = _now_ms() - send_started

            def _both_published() -> bool:
                return all(len(collector.for_task(s["task_id"])) >= 1 for s in specs)
            got, wait_elapsed = wait_for(_both_published, timeout_s=60.0,
                                         interval_s=1.0,
                                         what="provisional device results")
            results = {s["task_id"]: collector.for_task(s["task_id"]) for s in specs}
        finally:
            for publisher in publishers.values():
                publisher.__exit__(None, None, None)

    def _trace_ok(task_results: list[dict]) -> bool:
        return bool(task_results) and all(
            all(k in r["payload"] for k in
                ("trace_id", "task_id", "edge_node_id", "decision_round_id"))
            for r in task_results)

    assertions.update({
        "all_packets_published": not publish_errors,
        "provisional_result_task_1": len(results[specs[0]["task_id"]]) >= 1,
        "provisional_result_task_2": len(results[specs[1]["task_id"]]) >= 1,
        "trace_identity_present": all(_trace_ok(results[s["task_id"]]) for s in specs),
        "edges_remain_live": all(edge_health(b).get("liveness", {}).get("alive")
                                 is True for b in (EDGE_01_BASE, EDGE_02_BASE)),
    })
    h1 = edge_health(EDGE_01_BASE)
    return {
        "started_at_epoch_ms": started, "packets_sent": sent,
        "publish_errors": publish_errors[:5],
        "send_elapsed_ms": round(send_elapsed), "result_wait_ms": round(wait_elapsed),
        "decisions": {k: {"http": v["http"],
                          "target_topic": (v["decision"] or {}).get("target_topic")
                          if isinstance(v["decision"], dict) else None}
                      for k, v in decisions.items()},
        "device_results": {
            s["task_id"]: [{"decision_source": r["payload"].get("decision_source"),
                            "action_grade": r["payload"].get("action_grade"),
                            "trace_id": r["payload"].get("trace_id"),
                            "edge_node_id": r["payload"].get("edge_node_id")}
                           for r in results[s["task_id"]]] for s in specs},
        "mqtt_capacity_edge_01": h1.get("mqtt_capacity"),
        "scheduler": {"http": scheduler_status},
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


def scenario_model_queue_saturation(context: dict) -> dict:
    """队列满载（方案矩阵"队列满载"项）：推理挂起 + 80 包零间隔突发。

    观测路径：/health model_queue（waiting/capacity/max_observed_queued/
    queue_full_total）。满载包走"诊断不可用"降级（不伪造诊断），节点存活；
    恢复后队列排空、readiness 恢复。
    """
    fake: FakeModelService = context["fake"]
    base = EDGE_01_BASE
    task_id = "task_queue_sat_%s" % RUN_TOKEN
    before = edge_health(base).get("model_queue") or {}
    full_before = before.get("queue_full_total") or 0

    status, _ = _http("POST", base + "/edge/tasks", build_dispatch(
        task_id, "edge_01", "device_queue_sat", [("bearing_01", "sender_01")]))
    # timeout 模式仅挂起 /infer（/readiness 保持就绪），推理预算 1.5s 内不返回。
    fake.set_mode("timeout")
    publish_errors = []
    try:
        with MqttLinkPublisher("sender_01", "edge_01") as publisher:
            for sequence in range(1, PACKETS_PER_BEARING + 1):
                ok, err = publisher.publish(
                    build_packet("device_queue_sat", "bearing_01", "sender_01",
                                 task_id, sequence), timeout_s=5.0)
                if not ok:
                    publish_errors.append(err)
        during = edge_health(base).get("model_queue") or {}
        live_during = _safe_live(base)
    finally:
        fake.set_mode("ok")

    def _drained() -> bool:
        return (edge_health(base).get("model_queue") or {}).get("waiting") == 0
    drained, drain_ms = wait_for(_drained, timeout_s=30.0, interval_s=1.0,
                                 what="model queue drain")
    ready_ok, _ = wait_for(
        lambda: edge_health(base).get("readiness", {}).get("ready") is True,
        timeout_s=15.0, what="readiness after saturation recovery")
    after = edge_health(base).get("model_queue") or {}
    return {
        "assertions": {
            "task_accepted": status == 200,
            "burst_all_published": not publish_errors,
            "saturation_rejects_observed": (during.get("queue_full_total") or 0) > full_before,
            "queue_peak_observed": (during.get("max_observed_queued") or 0) >= 32,
            "edge_live_during_saturation": live_during,
            "queue_drained_after_recovery": drained,
            "readiness_recovered": ready_ok,
        },
        "model_queue": {"before": before, "during": during, "after": after},
        "drain_ms": round(drain_ms),
        "publish_errors": publish_errors[:3],
    }


SCENARIOS = {
    "task_flow_baseline_provisional": scenario_task_flow_baseline,
    "model_unavailable_isolation": scenario_model_unavailable_isolation,
    "sender_link_fault_isolation": scenario_sender_link_fault_isolation,
    "edge_node_restart_isolation": scenario_edge_node_restart_isolation,
    "duplicate_packet_stability": scenario_duplicate_packet_stability,
    "model_queue_saturation": scenario_model_queue_saturation,
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
