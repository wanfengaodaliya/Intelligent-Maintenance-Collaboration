import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


class MemoryLogSink:
    def __init__(self) -> None:
        self.packet_records: list[dict] = []

    def write_packet(self, record: dict) -> None:
        self.packet_records.append(record)


class FailingLogSink:
    def write_packet(self, record: dict) -> None:
        raise OSError("disk unavailable")


class FakeReasonCode:
    is_failure = False


class FakeMessageInfo:
    def __init__(self, mid: int, rc: int = 0) -> None:
        self.mid = mid
        self.rc = rc


class FakeMqttClient:
    def __init__(self, *, immediate_ack: bool = False) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_publish = None
        self.immediate_ack = immediate_ack
        self.next_mid = 1
        self.publish_call_count = 0

    def connect(self, host: str, port: int, keepalive: int) -> int:
        if self.on_connect:
            self.on_connect(self, None, {}, FakeReasonCode(), None)
        return 0

    def loop_start(self) -> None:
        return

    def loop_stop(self) -> None:
        return

    def disconnect(self) -> None:
        return

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> FakeMessageInfo:
        self.publish_call_count += 1
        mid = self.next_mid
        self.next_mid += 1
        if self.immediate_ack and self.on_publish:
            self.on_publish(self, None, mid, FakeReasonCode(), None)
        return FakeMessageInfo(mid)


class ExplicitlyFailingMqttClient(FakeMqttClient):
    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> FakeMessageInfo:
        self.publish_call_count += 1
        return FakeMessageInfo(self.publish_call_count, rc=4)


def sample_packet(sequence: int) -> dict:
    return {
        "device_id": "machine_01",
        "task_id": "sd_01_tk_0001",
        "bearing_id": "bearing_01",
        "packet_id": f"sd_01_tk_0001_bearing_01_pkt_{sequence:03d}",
        "sender_id": "sender_01",
        "sequence_number": sequence,
        "end_generate_timestamp_ns": time.time_ns(),
        "data": {},
    }


class MqttPublisherUnitTests(unittest.TestCase):
    def test_explicit_publish_failure_republishes_at_most_twice(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        client = ExplicitlyFailingMqttClient()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=20,
            delivery_timeout_ms=100,
            max_retries=2,
            queue_max_packets=80,
            log_sink=sink,
            client=client,
        )

        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher.wait_until_settled(0.5)
        publisher.stop()

        self.assertEqual(client.publish_call_count, 3)
        self.assertEqual(publisher.publish_retry_total, 2)
        self.assertEqual(sink.packet_records[0]["mqtt_publish_retry_count"], 2)
        self.assertEqual(sink.packet_records[0]["error_code"], "MQTT_NOT_CONNECTED")

    def test_reconnect_actively_republishes_pending_packet(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        client = FakeMqttClient()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=100,
            delivery_timeout_ms=500,
            max_retries=2,
            queue_max_packets=80,
            log_sink=sink,
            client=client,
        )

        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher._on_disconnect(client, None, {}, 1, None)
        publisher._on_connect(client, None, {}, FakeReasonCode(), None)
        publisher.stop()

        self.assertEqual(client.publish_call_count, 2)
        self.assertEqual(publisher.reconnect_count, 1)
        self.assertEqual(sink.packet_records[0]["mqtt_publish_retry_count"], 1)

    def test_disconnected_timeout_uses_not_connected_error(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        client = FakeMqttClient()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=10,
            delivery_timeout_ms=30,
            max_retries=0,
            queue_max_packets=80,
            log_sink=sink,
            client=client,
        )

        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher._on_disconnect(client, None, {}, 1, None)
        publisher.wait_until_settled(0.5)
        publisher.stop()

        self.assertEqual(sink.packet_records[0]["error_code"], "MQTT_NOT_CONNECTED")

    def test_packet_log_failure_does_not_break_mqtt_confirmation(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=20,
            delivery_timeout_ms=100,
            max_retries=2,
            queue_max_packets=80,
            log_sink=FailingLogSink(),
            client=FakeMqttClient(immediate_ack=True),
        )

        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        settled = publisher.wait_until_settled(0.5)
        publisher.stop()

        self.assertTrue(settled)
        self.assertEqual(publisher.status_counts["confirmed"], 1)
        self.assertEqual(len(publisher.logging_errors), 1)

    def test_immediate_puback_race_still_records_confirmed(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=20,
            delivery_timeout_ms=100,
            max_retries=2,
            queue_max_packets=80,
            log_sink=sink,
            client=FakeMqttClient(immediate_ack=True),
        )
        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher.wait_until_settled(0.5)
        publisher.stop()

        self.assertEqual(sink.packet_records[0]["publish_status"], "confirmed")
        self.assertIsNone(sink.packet_records[0]["error_code"])
        self.assertEqual(sink.packet_records[0]["device_id"], "machine_01")
        self.assertEqual(sink.packet_records[0]["sender_id"], "sender_01")
        self.assertEqual(sink.packet_records[0]["bearing_id"], "bearing_01")

    def test_unacknowledged_packet_fails_at_delivery_deadline(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=10,
            delivery_timeout_ms=30,
            max_retries=2,
            queue_max_packets=80,
            log_sink=sink,
            client=FakeMqttClient(),
        )
        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher.wait_until_settled(0.5)
        publisher.stop()

        self.assertEqual(sink.packet_records[0]["publish_status"], "failed")
        self.assertEqual(sink.packet_records[0]["error_code"], "PUBACK_TIMEOUT")
        self.assertIn("sd_01_tk_0001_bearing_01_pkt_001", publisher.warning_packet_ids)

    def test_full_queue_drops_oldest_pending_packet(self) -> None:
        from sender.mqtt_publisher import MqttPublisher

        sink = MemoryLogSink()
        publisher = MqttPublisher(
            sender_id="sender_01",
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=30,
            qos=1,
            retain=False,
            warning_timeout_ms=100,
            delivery_timeout_ms=300,
            max_retries=2,
            queue_max_packets=1,
            log_sink=sink,
            client=FakeMqttClient(),
        )
        publisher.start()
        publisher.publish(sample_packet(1), b"{}", "edge/edge_2/input")
        publisher.publish(sample_packet(2), b"{}", "edge/edge_2/input")
        publisher.stop()

        dropped = sink.packet_records[0]
        self.assertEqual(dropped["packet_id"], "sd_01_tk_0001_bearing_01_pkt_001")
        self.assertEqual(dropped["publish_status"], "dropped")
        self.assertEqual(dropped["error_code"], "SEND_QUEUE_FULL")


class RealMosquittoIntegrationTests(unittest.TestCase):
    def test_qos1_publish_reaches_local_broker_and_subscriber(self) -> None:
        try:
            with socket.create_connection(("127.0.0.1", 1883), timeout=0.5):
                pass
        except OSError:
            self.skipTest("local Mosquitto is not running")

        import paho.mqtt.client as mqtt
        from sender.local_logs import LocalLogSink
        from sender.mqtt_publisher import MqttPublisher

        topic = f"codex/sender-test/{time.time_ns()}"
        received = threading.Event()
        payloads: list[dict] = []
        subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        def on_message(client, userdata, message) -> None:
            payloads.append(json.loads(message.payload.decode("utf-8")))
            received.set()

        subscriber.on_message = on_message
        subscriber.connect("127.0.0.1", 1883, 30)
        subscriber.subscribe(topic, qos=1)
        subscriber.loop_start()
        time.sleep(0.1)

        with tempfile.TemporaryDirectory() as temp_dir:
            sink = LocalLogSink(Path(temp_dir))
            publisher = MqttPublisher(
                sender_id="sender_01",
                host="127.0.0.1",
                port=1883,
                keepalive_seconds=30,
                qos=1,
                retain=False,
                warning_timeout_ms=500,
                delivery_timeout_ms=1000,
                max_retries=2,
                queue_max_packets=80,
                log_sink=sink,
            )
            publisher.start()
            packet = sample_packet(1)
            encoded = json.dumps(packet).encode("utf-8")
            publisher.publish(packet, encoded, topic)
            publisher.wait_until_settled(2)
            publisher.stop()

            records = [json.loads(line) for line in sink.packet_path.read_text(encoding="utf-8").splitlines()]

        subscriber.disconnect()
        subscriber.loop_stop()
        self.assertTrue(received.wait(1))
        self.assertEqual(payloads[0]["packet_id"], "sd_01_tk_0001_bearing_01_pkt_001")
        self.assertEqual(records[0]["publish_status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
