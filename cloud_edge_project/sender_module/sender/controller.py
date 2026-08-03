from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import Any

from sender.config import SenderConfig
from sender.ids import TaskIdStore
from sender.local_logs import LocalLogSink
from sender.mat_reader import load_mat_record
from sender.mqtt_publisher import MqttPublisher
from sender.packet import build_sensor_packet, serialize_packet
from sender.scheduler_client import SchedulerClient, SchedulerError


def _task_status(counts: dict[str, int], expected: int) -> str:
    if counts.get("confirmed", 0) == expected:
        return "completed"
    if counts.get("confirmed", 0) > 0:
        return "partially_completed"
    return "failed"


def run_task(
    config: SenderConfig,
    mat_path: Path | str,
    *,
    realtime: bool = True,
    scheduler: Any | None = None,
    publisher: Any | None = None,
    log_sink: LocalLogSink | None = None,
    task_ids: TaskIdStore | None = None,
) -> dict[str, Any]:
    sink = log_sink or LocalLogSink(config.log_dir)
    id_store = task_ids or TaskIdStore(config.state_dir / "task_counter.txt")
    scheduler_client = scheduler or SchedulerClient(
        url=config.scheduler_url,
        timeout_seconds=config.scheduler_timeout_seconds,
        max_retries=config.schedule_max_retries,
    )

    task_id = id_store.next_task_id()
    task_started_timestamp_ns = time.time_ns()
    record = load_mat_record(mat_path)
    windows = record.windows(
        duration_ms=config.packet_interval_ms,
        count=config.expected_packet_count,
    )
    try:
        first_window = next(windows)
    except StopIteration as exc:
        raise RuntimeError("MAT record produced no data windows") from exc

    preview_packet = build_sensor_packet(
        task_id=task_id,
        sender_id=config.sender_id,
        sequence_number=first_window.sequence_number,
        data=first_window.data,
        end_generate_timestamp_ns=task_started_timestamp_ns,
    )
    schedule_request = {
        "task_id": task_id,
        "sender_id": config.sender_id,
        "packet_size_bytes": len(serialize_packet(preview_packet)),
        "expected_duration_ms": config.task_duration_ms,
        "created_timestamp_ns": task_started_timestamp_ns,
    }

    try:
        assignment = scheduler_client.assign(schedule_request)
    except SchedulerError as exc:
        summary = {
            "task_id": task_id,
            "sender_id": config.sender_id,
            "target_topic": None,
            "expected_packet_count": config.expected_packet_count,
            "schedule_retry_count": exc.retry_count,
            "mqtt_reconnect_count": 0,
            "mqtt_publish_retry_total": 0,
            "task_started_timestamp_ns": task_started_timestamp_ns,
            "task_finished_timestamp_ns": time.time_ns(),
            "task_status": "scheduling_failed",
            "replay_mode": "realtime" if realtime else "accelerated",
        }
        sink.write_task(summary)
        raise

    mqtt_publisher = publisher or MqttPublisher(
        sender_id=config.sender_id,
        host=config.mqtt_host,
        port=config.mqtt_port,
        keepalive_seconds=config.mqtt_keepalive_seconds,
        qos=config.qos,
        retain=config.retain,
        warning_timeout_ms=config.puback_warning_timeout_ms,
        delivery_timeout_ms=config.packet_delivery_timeout_ms,
        max_retries=config.max_publish_retries,
        queue_max_packets=config.pending_queue_max_packets,
        log_sink=sink,
    )

    try:
        mqtt_publisher.start()
        replay_started = time.monotonic()
        interval_seconds = config.packet_interval_ms / 1000.0
        for window in itertools.chain([first_window], windows):
            if realtime:
                due = replay_started + (window.sequence_number - 1) * interval_seconds
                remaining = due - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)

            packet = build_sensor_packet(
                task_id=task_id,
                sender_id=config.sender_id,
                sequence_number=window.sequence_number,
                data=window.data,
                end_generate_timestamp_ns=time.time_ns(),
            )
            payload = serialize_packet(packet)
            mqtt_publisher.publish(packet, payload, assignment.target_topic)

        mqtt_publisher.wait_until_settled(
            config.packet_delivery_timeout_ms / 1000.0 + 0.5
        )
    except Exception as exc:
        try:
            mqtt_publisher.stop()
        except Exception:
            pass
        summary = {
            "task_id": task_id,
            "sender_id": config.sender_id,
            "target_topic": assignment.target_topic,
            "expected_packet_count": config.expected_packet_count,
            "schedule_retry_count": assignment.schedule_retry_count,
            "mqtt_reconnect_count": mqtt_publisher.reconnect_count,
            "mqtt_publish_retry_total": mqtt_publisher.publish_retry_total,
            "task_started_timestamp_ns": task_started_timestamp_ns,
            "task_finished_timestamp_ns": time.time_ns(),
            "task_status": "failed",
            "replay_mode": "realtime" if realtime else "accelerated",
            "error_code": "MQTT_TASK_ERROR",
            "error_message": str(exc),
        }
        sink.write_task(summary)
        raise
    else:
        mqtt_publisher.stop()

    counts = mqtt_publisher.status_counts
    summary = {
        "task_id": task_id,
        "sender_id": config.sender_id,
        "target_topic": assignment.target_topic,
        "expected_packet_count": config.expected_packet_count,
        "schedule_retry_count": assignment.schedule_retry_count,
        "mqtt_reconnect_count": mqtt_publisher.reconnect_count,
        "mqtt_publish_retry_total": mqtt_publisher.publish_retry_total,
        "task_started_timestamp_ns": task_started_timestamp_ns,
        "task_finished_timestamp_ns": time.time_ns(),
        "task_status": _task_status(counts, config.expected_packet_count),
        "replay_mode": "realtime" if realtime else "accelerated",
    }
    sink.write_task(summary)
    return summary
