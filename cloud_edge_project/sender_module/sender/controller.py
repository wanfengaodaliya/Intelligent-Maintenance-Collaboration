from __future__ import annotations

import itertools
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sender.config import SenderConfig
from sender.ids import TaskIdStore
from sender.local_logs import LocalLogSink
from sender.mat_reader import load_mat_record
from sender.mqtt_publisher import MqttPublisher
from sender.packet import build_sensor_packet, serialize_packet
from sender.scheduler_client import ScheduleAssignment, SchedulerClient, SchedulerError


def _task_status(counts: dict[str, int], expected: int) -> str:
    if counts.get("confirmed", 0) == expected:
        return "completed"
    if counts.get("confirmed", 0) > 0:
        return "partially_completed"
    return "failed"


def _assignment_records(assignment: ScheduleAssignment) -> list[dict[str, str]]:
    return [
        {
            "bearing_id": item.bearing_id,
            "target_topic": item.target_topic,
        }
        for item in assignment.assignments
    ]


def run_task(
    config: SenderConfig,
    device_id: str,
    bearing_files: Mapping[str, Path | str],
    *,
    realtime: bool = True,
    scheduler: Any | None = None,
    publisher: Any | None = None,
    log_sink: LocalLogSink | None = None,
    task_ids: TaskIdStore | None = None,
) -> dict[str, Any]:
    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device_id cannot be empty")
    if len(bearing_files) != 3:
        raise ValueError("one task requires exactly three bearings")

    bearing_ids = list(bearing_files)
    sink = log_sink or LocalLogSink(config.log_dir)
    id_store = task_ids or TaskIdStore(config.state_dir / "task_counter.txt")
    scheduler_client = scheduler or SchedulerClient(
        url=config.scheduler_url,
        timeout_seconds=config.scheduler_timeout_seconds,
        max_retries=config.schedule_max_retries,
    )

    task_id = id_store.next_task_id()
    task_started_timestamp_ns = time.time_ns()
    windows_by_bearing: dict[str, Any] = {}
    first_windows: dict[str, Any] = {}
    for bearing_id, mat_path in bearing_files.items():
        record = load_mat_record(mat_path)
        windows = record.windows(
            duration_ms=config.packet_interval_ms,
            count=config.expected_packet_count,
        )
        try:
            first_window = next(windows)
        except StopIteration as exc:
            raise RuntimeError(f"MAT record produced no data windows: {bearing_id}") from exc
        first_windows[bearing_id] = first_window
        windows_by_bearing[bearing_id] = itertools.chain([first_window], windows)

    preview_packets = {
        bearing_id: build_sensor_packet(
            device_id=device_id,
            task_id=task_id,
            bearing_id=bearing_id,
            sender_id=config.sender_id,
            sequence_number=first_windows[bearing_id].sequence_number,
            data=first_windows[bearing_id].data,
            end_generate_timestamp_ns=task_started_timestamp_ns,
        )
        for bearing_id in bearing_ids
    }
    schedule_request = {
        "device_id": device_id,
        "task_id": task_id,
        "sender_id": config.sender_id,
        "bearings": [
            {
                "bearing_id": bearing_id,
                "packet_size_bytes": len(serialize_packet(preview_packets[bearing_id])),
            }
            for bearing_id in bearing_ids
        ],
        "expected_duration_ms": config.task_duration_ms,
        "created_timestamp_ns": task_started_timestamp_ns,
    }

    expected_total = config.expected_packet_count * len(bearing_ids)
    try:
        assignment = scheduler_client.assign(schedule_request)
    except SchedulerError as exc:
        summary = {
            "device_id": device_id,
            "task_id": task_id,
            "sender_id": config.sender_id,
            "assignments": [],
            "expected_bearing_count": len(bearing_ids),
            "expected_packet_count_per_bearing": config.expected_packet_count,
            "expected_packet_total": expected_total,
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

    assignment_records = _assignment_records(assignment)
    try:
        mqtt_publisher.start()
        replay_started = time.monotonic()
        interval_seconds = config.packet_interval_ms / 1000.0
        for sequence_number in range(1, config.expected_packet_count + 1):
            if realtime:
                due = replay_started + (sequence_number - 1) * interval_seconds
                remaining = due - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)

            for bearing_id in bearing_ids:
                try:
                    window = next(windows_by_bearing[bearing_id])
                except StopIteration as exc:
                    raise RuntimeError(
                        f"bearing record ended before packet {sequence_number}: {bearing_id}"
                    ) from exc
                if window.sequence_number != sequence_number:
                    raise RuntimeError(f"bearing window sequence mismatch: {bearing_id}")
                packet = build_sensor_packet(
                    device_id=device_id,
                    task_id=task_id,
                    bearing_id=bearing_id,
                    sender_id=config.sender_id,
                    sequence_number=sequence_number,
                    data=window.data,
                    end_generate_timestamp_ns=time.time_ns(),
                )
                mqtt_publisher.publish(
                    packet,
                    serialize_packet(packet),
                    assignment.topic_for(bearing_id),
                )

        mqtt_publisher.wait_until_settled(
            config.packet_delivery_timeout_ms / 1000.0 + 0.5
        )
    except Exception as exc:
        try:
            mqtt_publisher.stop()
        except Exception:
            pass
        summary = {
            "device_id": device_id,
            "task_id": task_id,
            "sender_id": config.sender_id,
            "assignments": assignment_records,
            "expected_bearing_count": len(bearing_ids),
            "expected_packet_count_per_bearing": config.expected_packet_count,
            "expected_packet_total": expected_total,
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
        "device_id": device_id,
        "task_id": task_id,
        "sender_id": config.sender_id,
        "assignments": assignment_records,
        "expected_bearing_count": len(bearing_ids),
        "expected_packet_count_per_bearing": config.expected_packet_count,
        "expected_packet_total": expected_total,
        "schedule_retry_count": assignment.schedule_retry_count,
        "mqtt_reconnect_count": mqtt_publisher.reconnect_count,
        "mqtt_publish_retry_total": mqtt_publisher.publish_retry_total,
        "task_started_timestamp_ns": task_started_timestamp_ns,
        "task_finished_timestamp_ns": time.time_ns(),
        "task_status": _task_status(counts, expected_total),
        "replay_mode": "realtime" if realtime else "accelerated",
    }
    sink.write_task(summary)
    return summary
