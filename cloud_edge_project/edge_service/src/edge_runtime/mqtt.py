# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable, Mapping, Optional

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .json_utils import json_bytes


MQTT_MAX_PAYLOAD_BYTES = 512 * 1024


class MqttRuntimeError(RuntimeError):
    pass


class MqttIngress:
    def __init__(
        self,
        config: MqttConfig,
        on_packet: Callable[[dict[str, Any]], None],
        *,
        on_error: Optional[Callable[[dict[str, Any]], None]] = None,
        client: Any = None,
        max_payload_bytes: int = MQTT_MAX_PAYLOAD_BYTES,
    ):
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self.config = config
        self.on_packet = on_packet
        self.on_error = on_error or (lambda _: None)
        self.client = client or mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        self.max_payload_bytes = max_payload_bytes
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        if hasattr(self.client, "manual_ack_set"):
            self.client.manual_ack_set(True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        # 队列元素为 (packet, message, enqueued_at_ns)，时间戳用于最老任务年龄指标。
        self._queue: queue.Queue[tuple[dict[str, Any], Any, int]] = queue.Queue(
            maxsize=config.ingress_queue_capacity
        )
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._connected = threading.Event()
        # 阶段 5：满载拒绝计数，配合断连背压构成可观测的过载策略。
        self._rejected_total = 0
        self._oversized_total = 0
        self._rejected_lock = threading.Lock()

    def start(self, *, connect_timeout_seconds: float = 5.0) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._consume, name="edge-mqtt-ingress", daemon=True)
        self._worker.start()
        try:
            self.client.connect(
                self.config.host, self.config.port, self.config.keepalive_seconds
            )
            self.client.loop_start()
        except Exception:
            self._stop_worker()
            raise
        if not self._connected.wait(connect_timeout_seconds):
            self.stop()
            raise MqttRuntimeError("MQTT connection timed out")

    def stop(self) -> None:
        self._stop.set()
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self._stop_worker()
            self._connected.clear()

    def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.join(timeout=2.0)

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            self._emit_error("MQTT_CONNECT_FAILED", str(reason_code))
            return
        result, _ = client.subscribe(self.config.input_topic, qos=self.config.qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._emit_error("MQTT_SUBSCRIBE_FAILED", str(result))
            return
        self._connected.set()

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connected.clear()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        if len(message.payload) > self.max_payload_bytes:
            with self._rejected_lock:
                self._oversized_total += 1
            self._emit_error(
                "MQTT_PAYLOAD_TOO_LARGE",
                "MQTT payload exceeds the configured limit",
            )
            self._ack(message)
            return
        try:
            value = json.loads(message.payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("sensor packet must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._emit_error("INVALID_MQTT_PACKET", str(exc))
            self._ack(message)
            return
        try:
            self._queue.put((value, message, time.time_ns()), timeout=1.0)
        except queue.Full:
            # 满载策略：断连背压（QoS1 由 broker 重发构成可重试语义），
            # 拒绝必须计数，禁止静默丢弃。
            with self._rejected_lock:
                self._rejected_total += 1
            self._emit_error("INGRESS_QUEUE_FULL", "edge MQTT ingress queue is full")
            client.disconnect()
            return

    def _ack(self, message: Any) -> None:
        if hasattr(self.client, "ack"):
            result = self.client.ack(message.mid, message.qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._emit_error("MQTT_ACK_FAILED", str(result))

    def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                packet, message, _enqueued_at_ns = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                accepted = self.on_packet(packet)
                if accepted is not False:
                    self._ack(message)
            except Exception as exc:
                self._emit_error("PACKET_HANDLER_FAILED", repr(exc))
            finally:
                self._queue.task_done()

    def _emit_error(self, code: str, message: str) -> None:
        try:
            self.on_error({"error_code": code, "message": message, "stage": "mqtt_ingress"})
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def worker_alive(self) -> bool:
        """入站消费线程是否存活（liveness 判定依据之一）。"""
        return self._worker is not None and self._worker.is_alive()

    @property
    def rejected_total(self) -> int:
        """满载拒绝累计数；持续增长说明容量配置低于实际流量。"""
        with self._rejected_lock:
            return self._rejected_total

    @property
    def oversized_total(self) -> int:
        """Payloads rejected before JSON decoding because they exceed the limit."""
        with self._rejected_lock:
            return self._oversized_total

    @property
    def oldest_task_age_ms(self) -> float | None:
        """队首任务等待毫秒数；空队列返回 None。"""
        head = getattr(self._queue, "queue", None)
        if not head:
            return None
        oldest = head[0]
        return max((time.time_ns() - oldest[2]) / 1_000_000.0, 0.0)

    def capacity_snapshot(self) -> dict[str, Any]:
        """阶段 5：入站容量指标快照，供健康接口与状态上报使用。"""
        return {
            "queue_depth": self.queue_depth,
            "queue_capacity": self.config.ingress_queue_capacity,
            "rejected_total": self.rejected_total,
            "oversized_total": self.oversized_total,
            "max_payload_bytes": self.max_payload_bytes,
            "oldest_task_age_ms": self.oldest_task_age_ms,
            "connected": self.connected,
            "worker_alive": self.worker_alive,
        }


class MqttJsonPublisher:
    def __init__(self, client: Any, *, topic: str, qos: int = 1):
        self.client = client
        self.topic = topic
        self.qos = qos
        self._lock = threading.Lock()

    def publish(self, payload: Mapping[str, Any], *, timeout_seconds: float = 2.0) -> None:
        with self._lock:
            info = self.client.publish(
                self.topic, payload=json_bytes(dict(payload)), qos=self.qos, retain=False
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise MqttRuntimeError("MQTT publish failed: %s" % info.rc)
            info.wait_for_publish(timeout=timeout_seconds)
            if not info.is_published():
                raise MqttRuntimeError("MQTT publish acknowledgement timed out")
