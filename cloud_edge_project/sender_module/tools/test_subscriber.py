from __future__ import annotations

import argparse
import json
import threading

import paho.mqtt.client as mqtt


def main() -> None:
    parser = argparse.ArgumentParser(description="Count unique SensorPacket messages")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic", default="edge/+/input")
    parser.add_argument("--expected", type=int, default=240)
    args = parser.parse_args()

    completed = threading.Event()
    packet_ids: set[str] = set()
    bearing_counts: dict[str, int] = {}
    sender_counts: dict[str, int] = {}

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            raise RuntimeError(f"MQTT connection failed: {reason_code}")
        client.subscribe(args.topic, qos=1)
        print(f"subscribed: {args.topic}")

    def on_message(client, userdata, message) -> None:
        packet = json.loads(message.payload.decode("utf-8"))
        packet_ids.add(packet["packet_id"])
        bearing_id = packet["bearing_id"]
        bearing_counts[bearing_id] = bearing_counts.get(bearing_id, 0) + 1
        sender_id = packet["sender_id"]
        sender_counts[sender_id] = sender_counts.get(sender_id, 0) + 1
        print(
            f"received {len(packet_ids)}/{args.expected}: {packet['packet_id']} "
            f"sender_counts={sender_counts} bearing_counts={bearing_counts}"
        )
        if len(packet_ids) >= args.expected:
            completed.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, 30)
    client.loop_start()
    try:
        completed.wait()
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()

