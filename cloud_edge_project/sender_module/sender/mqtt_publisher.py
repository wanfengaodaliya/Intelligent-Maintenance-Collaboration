from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import paho.mqtt.client as mqtt


class PacketLogSink(Protocol):
    def write_packet(self, record: dict[str, Any]) -> None: ...


class MqttPublisherError(RuntimeError):
    pass


@dataclass
class PendingMessage:
    packet: dict[str, Any]
    payload: bytes
    topic: str
    first_publish_monotonic_ns: int
    last_publish_monotonic_ns: int
    mqtt_publish_timestamp_ns: int
    retry_count: int = 0
    mids: set[int] = field(default_factory=set)
    warned: bool = False
    initial_publish_error: bool = False


class MqttPublisher:
    def __init__(
        self,
        *,
        sender_id: str,
        host: str,
        port: int,
        keepalive_seconds: int,
        qos: int,
        retain: bool,
        warning_timeout_ms: int,
        delivery_timeout_ms: int,
        max_retries: int,
        queue_max_packets: int,
        log_sink: PacketLogSink,
        client: Any | None = None,
    ) -> None:
        self.sender_id = sender_id
        self.host = host
        self.port = port
        self.keepalive_seconds = keepalive_seconds
        self.qos = qos
        self.retain = retain
        self.warning_timeout_ns = warning_timeout_ms * 1_000_000
        self.delivery_timeout_ns = delivery_timeout_ms * 1_000_000
        self.max_retries = max_retries
        self.queue_max_packets = queue_max_packets
        self.log_sink = log_sink

        self.client = client or mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=sender_id,
            protocol=mqtt.MQTTv311,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish

        self._condition = threading.Condition()
        self._pending: OrderedDict[str, PendingMessage] = OrderedDict()
        self._mid_to_packet: dict[int, str] = {}
        self._early_acks: set[int] = set()
        self._retired_mids: set[int] = set()
        self._connected = threading.Event()
        self._stop_monitor = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._started = False
        self._has_connected_once = False
        self._reconnect_count = 0
        self._publish_retry_total = 0
        self._status_counts = {"confirmed": 0, "failed": 0, "dropped": 0}
        self.warning_packet_ids: set[str] = set()
        self._logging_errors: list[str] = []

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def publish_retry_total(self) -> int:
        return self._publish_retry_total

    @property
    def status_counts(self) -> dict[str, int]:
        return dict(self._status_counts)

    @property
    def logging_errors(self) -> tuple[str, ...]:
        return tuple(self._logging_errors)

    def start(self, connect_timeout_seconds: float = 3.0) -> None:
        if self._started:
            return
        self._started = True
        try:
            rc = self.client.connect(self.host, self.port, self.keepalive_seconds)
        except Exception as exc:
            self._started = False
            raise MqttPublisherError(f"cannot connect to MQTT Broker: {exc}") from exc
        if rc not in (None, mqtt.MQTT_ERR_SUCCESS):
            self._started = False
            raise MqttPublisherError(f"MQTT connect returned error code {rc}")

        self.client.loop_start()
        if not self._connected.wait(connect_timeout_seconds):
            self.client.loop_stop()
            self._started = False
            raise MqttPublisherError("MQTT connection timed out")

        self._monitor_thread = threading.Thread(
            target=self._monitor_pending,
            name="sender-mqtt-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def publish(self, packet: dict[str, Any], payload: bytes, topic: str) -> None:
        if not self._started:
            raise MqttPublisherError("MQTT publisher is not started")
        packet_id = str(packet.get("packet_id", ""))
        if not packet_id:
            raise MqttPublisherError("packet_id is required")

        dropped_record: dict[str, Any] | None = None
        with self._condition:
            if packet_id in self._pending:
                raise MqttPublisherError(f"packet is already pending: {packet_id}")
            if len(self._pending) >= self.queue_max_packets:
                oldest_id = next(iter(self._pending))
                dropped_record = self._finalize_locked(
                    oldest_id,
                    status="dropped",
                    error_code="SEND_QUEUE_FULL",
                    broker_ack_timestamp_ns=None,
                )

            pending = PendingMessage(
                packet=packet,
                payload=payload,
                topic=topic,
                first_publish_monotonic_ns=time.monotonic_ns(),
                last_publish_monotonic_ns=time.monotonic_ns(),
                mqtt_publish_timestamp_ns=time.time_ns(),
            )
            self._pending[packet_id] = pending

        if dropped_record is not None:
            self._write_packet_log(dropped_record)

        if not self._attempt_publish(packet_id, is_retry=False):
            self._retry_after_explicit_failure(packet_id)

    def _attempt_publish(self, packet_id: str, *, is_retry: bool) -> bool:
        with self._condition:
            pending = self._pending.get(packet_id)
            if pending is None:
                return True
            if is_retry:
                if pending.retry_count >= self.max_retries:
                    return False
                pending.retry_count += 1
                self._publish_retry_total += 1
            topic = pending.topic
            payload = pending.payload

        try:
            info = self.client.publish(
                topic,
                payload=payload,
                qos=self.qos,
                retain=self.retain,
            )
        except Exception:
            with self._condition:
                current = self._pending.get(packet_id)
                if current is not None:
                    current.initial_publish_error = True
            return False

        confirmed_record: dict[str, Any] | None = None
        with self._condition:
            current = self._pending.get(packet_id)
            if current is None:
                return True
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                current.initial_publish_error = True
                return False

            mid = int(info.mid)
            current.initial_publish_error = False
            current.last_publish_monotonic_ns = time.monotonic_ns()
            current.mids.add(mid)
            self._mid_to_packet[mid] = packet_id
            if mid in self._early_acks:
                self._early_acks.remove(mid)
                confirmed_record = self._finalize_locked(
                    packet_id,
                    status="confirmed",
                    error_code=None,
                    broker_ack_timestamp_ns=time.time_ns(),
                )
            self._condition.notify_all()

        if confirmed_record is not None:
            self._write_packet_log(confirmed_record)
        return True

    def _retry_after_explicit_failure(self, packet_id: str) -> None:
        while True:
            with self._condition:
                pending = self._pending.get(packet_id)
                if pending is None:
                    return
                age = time.monotonic_ns() - pending.first_publish_monotonic_ns
                exhausted = (
                    pending.retry_count >= self.max_retries
                    or age >= self.delivery_timeout_ns
                )
                if exhausted:
                    record = self._finalize_locked(
                        packet_id,
                        status="failed",
                        error_code="MQTT_NOT_CONNECTED",
                        broker_ack_timestamp_ns=None,
                    )
                    break
            if self._attempt_publish(packet_id, is_retry=True):
                return
        self._write_packet_log(record)

    def wait_until_settled(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_monitor.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2)

        records: list[dict[str, Any]] = []
        with self._condition:
            for packet_id in list(self._pending):
                records.append(
                    self._finalize_locked(
                        packet_id,
                        status="failed",
                        error_code=(
                            "PUBACK_TIMEOUT"
                            if self._connected.is_set()
                            else "MQTT_NOT_CONNECTED"
                        ),
                        broker_ack_timestamp_ns=None,
                    )
                )
        for record in records:
            self._write_packet_log(record)

        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self._started = False

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            return

        recovery_packet_ids: list[str] = []
        with self._condition:
            if self._has_connected_once:
                self._reconnect_count += 1
                recovery_packet_ids = list(self._pending)
            self._has_connected_once = True
            self._connected.set()
            self._condition.notify_all()

        for packet_id in recovery_packet_ids:
            self._retry_after_explicit_failure(packet_id)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        self._connected.clear()

    def _on_publish(
        self,
        client: Any,
        userdata: Any,
        mid: int,
        reason_code: Any,
        properties: Any,
    ) -> None:
        record: dict[str, Any] | None = None
        with self._condition:
            packet_id = self._mid_to_packet.get(mid)
            if packet_id is None:
                if mid not in self._retired_mids:
                    self._early_acks.add(mid)
                return
            record = self._finalize_locked(
                packet_id,
                status="confirmed",
                error_code=None,
                broker_ack_timestamp_ns=time.time_ns(),
            )
        if record is not None:
            self._write_packet_log(record)

    def _monitor_pending(self) -> None:
        while not self._stop_monitor.wait(0.01):
            now = time.monotonic_ns()
            expired_records: list[dict[str, Any]] = []
            retry_packet_ids: list[str] = []
            with self._condition:
                for packet_id, pending in list(self._pending.items()):
                    age = now - pending.first_publish_monotonic_ns
                    attempt_age = now - pending.last_publish_monotonic_ns
                    if age >= self.warning_timeout_ns and not pending.warned:
                        pending.warned = True
                        self.warning_packet_ids.add(packet_id)
                    if (
                        pending.retry_count < self.max_retries
                        and attempt_age >= self.warning_timeout_ns
                    ):
                        retry_packet_ids.append(packet_id)
                    elif (
                        pending.retry_count >= self.max_retries
                        and attempt_age >= self.delivery_timeout_ns
                    ):
                        error_code = (
                            "MQTT_NOT_CONNECTED"
                            if pending.initial_publish_error or not self._connected.is_set()
                            else "PUBACK_TIMEOUT"
                        )
                        expired_records.append(
                            self._finalize_locked(
                                packet_id,
                                status="failed",
                                error_code=error_code,
                                broker_ack_timestamp_ns=None,
                            )
                        )
            for packet_id in retry_packet_ids:
                self._attempt_publish(packet_id, is_retry=True)
            for record in expired_records:
                self._write_packet_log(record)

    def _write_packet_log(self, record: dict[str, Any]) -> None:
        try:
            self.log_sink.write_packet(record)
        except Exception as exc:
            # Logging is observability only; it must not interrupt MQTT delivery.
            self._logging_errors.append(f"{type(exc).__name__}: {exc}")

    def _finalize_locked(
        self,
        packet_id: str,
        *,
        status: str,
        error_code: str | None,
        broker_ack_timestamp_ns: int | None,
    ) -> dict[str, Any]:
        pending = self._pending.pop(packet_id)
        for mid in pending.mids:
            self._mid_to_packet.pop(mid, None)
            self._retired_mids.add(mid)
        self._status_counts[status] += 1
        self._condition.notify_all()
        return {
            "task_id": pending.packet["task_id"],
            "device_id": pending.packet["device_id"],
            "sender_id": pending.packet["sender_id"],
            "bearing_id": pending.packet["bearing_id"],
            "packet_id": pending.packet["packet_id"],
            "sequence_number": pending.packet["sequence_number"],
            "packet_size_bytes": len(pending.payload),
            "end_generate_timestamp_ns": pending.packet["end_generate_timestamp_ns"],
            "mqtt_publish_timestamp_ns": pending.mqtt_publish_timestamp_ns,
            "broker_ack_timestamp_ns": broker_ack_timestamp_ns,
            "mqtt_publish_retry_count": pending.retry_count,
            "publish_status": status,
            "error_code": error_code,
        }
