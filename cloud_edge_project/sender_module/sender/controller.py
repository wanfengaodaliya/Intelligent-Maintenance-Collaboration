from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sender.config import SenderConfig, SenderNodeConfig
from sender.ids import TaskIdStore
from sender.local_logs import LocalLogSink
from sender.mat_reader import load_mat_record
from sender.mqtt_publisher import MqttPublisher
from sender.packet import build_sensor_packet, serialize_packet
from sender.scheduler_client import SchedulerClient, SchedulerError
from sender.source_mapping import PacketSourceMappingStore


class SenderTaskError(RuntimeError):
    def __init__(self, summary: dict[str, Any], cause: Exception) -> None:
        super().__init__(str(cause))
        self.summary = summary


def _task_status(counts: dict[str, int], expected: int) -> str:
    confirmed = counts.get("confirmed", 0)
    if confirmed == expected:
        return "completed"
    if confirmed > 0:
        return "partially_completed"
    return "failed"


def _delivery_error_code(status: str) -> str | None:
    if status == "completed":
        return None
    if status == "partially_completed":
        return "PACKET_DELIVERY_PARTIAL"
    return "PACKET_DELIVERY_FAILED"


def _summary(
    *,
    config: SenderConfig,
    node: SenderNodeConfig,
    task_id: str,
    target_topic: str | None,
    schedule_retry_count: int,
    mqtt_reconnect_count: int,
    mqtt_publish_retry_total: int,
    counts: dict[str, int],
    started_ns: int,
    status: str,
    realtime: bool,
    error_code: str | None,
    error_message: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "device_id": config.device_id,
        "sender_id": node.sender_id,
        "bearing_id": node.bearing_id,
        "task_id": task_id,
        "target_topic": target_topic,
        "expected_packet_count": config.expected_packet_count,
        "confirmed_packet_count": counts.get("confirmed", 0),
        "failed_packet_count": counts.get("failed", 0),
        "dropped_packet_count": counts.get("dropped", 0),
        "schedule_retry_count": schedule_retry_count,
        "mqtt_reconnect_count": mqtt_reconnect_count,
        "mqtt_publish_retry_total": mqtt_publish_retry_total,
        "task_started_timestamp_ns": started_ns,
        "task_finished_timestamp_ns": time.time_ns(),
        "task_status": status,
        "replay_mode": "realtime" if realtime else "accelerated",
        "error_code": error_code,
    }
    if error_message is not None:
        record["error_message"] = error_message
    return record


def run_sender_task(
    config: SenderConfig,
    node: SenderNodeConfig,
    source_path: Path | str,
    *,
    realtime: bool = True,
    scheduler: Any | None = None,
    publisher: Any | None = None,
    log_sink: LocalLogSink | None = None,
    task_ids: TaskIdStore | None = None,
    source_mapping_store: PacketSourceMappingStore | None = None,
) -> dict[str, Any]:
    sink = log_sink or LocalLogSink(config.log_dir)
    id_store = task_ids or TaskIdStore(
        config.state_dir / f"{node.sender_id}_task_counter.txt",
        node.sender_id,
    )
    scheduler_client = scheduler or SchedulerClient(
        url=node.scheduler_url,
        timeout_seconds=config.scheduler_timeout_seconds,
        max_retries=config.schedule_max_retries,
    )

    task_id = id_store.next_task_id()
    source_store = source_mapping_store or PacketSourceMappingStore(
        config.state_dir / "packet_source_mapping.db"
    )
    started_ns = time.time_ns()
    record = load_mat_record(source_path)
    windows = record.windows(
        duration_ms=config.packet_interval_ms,
        count=config.expected_packet_count,
    )
    try:
        first_window = next(windows)
    except StopIteration as exc:
        raise RuntimeError(f"MAT record produced no data windows: {node.bearing_id}") from exc
    windows = itertools.chain([first_window], windows)

    preview_packet = build_sensor_packet(
        device_id=config.device_id,
        task_id=task_id,
        bearing_id=node.bearing_id,
        sender_id=node.sender_id,
        sequence_number=first_window.sequence_number,
        data=first_window.data,
        end_generate_timestamp_ns=started_ns,
    )
    schedule_request = {
        "device_id": config.device_id,
        "sender_id": node.sender_id,
        "task_id": task_id,
        "bearing_id": node.bearing_id,
        "packet_size_bytes": len(serialize_packet(preview_packet)),
        "expected_packet_count": config.expected_packet_count,
        "expected_duration_ms": config.task_duration_ms,
        "created_timestamp_ns": started_ns,
    }

    try:
        assignment = scheduler_client.assign(schedule_request)
    except SchedulerError as exc:
        summary = _summary(
            config=config,
            node=node,
            task_id=task_id,
            target_topic=None,
            schedule_retry_count=exc.retry_count,
            mqtt_reconnect_count=0,
            mqtt_publish_retry_total=0,
            counts={},
            started_ns=started_ns,
            status="scheduling_failed",
            realtime=realtime,
            error_code="SCHEDULER_REQUEST_FAILED",
            error_message=str(exc),
        )
        sink.write_task(summary)
        raise SenderTaskError(summary, exc) from exc

    mqtt_publisher = publisher or MqttPublisher(
        sender_id=node.sender_id,
        host=node.mqtt_host,
        port=node.mqtt_port,
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
        for sequence_number in range(1, config.expected_packet_count + 1):
            if realtime:
                due = replay_started + (sequence_number - 1) * interval_seconds
                remaining = due - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
            try:
                window = next(windows)
            except StopIteration as exc:
                raise RuntimeError(
                    f"MAT record ended before packet {sequence_number}: {node.bearing_id}"
                ) from exc
            if window.sequence_number != sequence_number:
                raise RuntimeError(f"bearing window sequence mismatch: {node.bearing_id}")
            packet = build_sensor_packet(
                device_id=config.device_id,
                task_id=task_id,
                bearing_id=node.bearing_id,
                sender_id=node.sender_id,
                sequence_number=sequence_number,
                data=window.data,
                end_generate_timestamp_ns=(
                    started_ns
                    + (sequence_number - 1) * config.packet_interval_ms * 1_000_000
                ),
            )
            source_store.save(
                packet_id=packet["packet_id"],
                task_id=task_id,
                bearing_id=node.bearing_id,
                source_path=record.source_path,
                start_index=window.start_index,
                end_index=window.end_index,
                window_index=window.window_index,
            )
            mqtt_publisher.publish(packet, serialize_packet(packet), assignment.target_topic)

        mqtt_publisher.wait_until_settled(config.packet_delivery_timeout_ms / 1000.0 + 0.5)
    except Exception as exc:
        try:
            mqtt_publisher.stop()
        except Exception:
            pass
        summary = _summary(
            config=config,
            node=node,
            task_id=task_id,
            target_topic=assignment.target_topic,
            schedule_retry_count=assignment.schedule_retry_count,
            mqtt_reconnect_count=mqtt_publisher.reconnect_count,
            mqtt_publish_retry_total=mqtt_publisher.publish_retry_total,
            counts=mqtt_publisher.status_counts,
            started_ns=started_ns,
            status="failed",
            realtime=realtime,
            error_code="MQTT_TASK_ERROR",
            error_message=str(exc),
        )
        sink.write_task(summary)
        raise SenderTaskError(summary, exc) from exc
    else:
        mqtt_publisher.stop()

    counts = mqtt_publisher.status_counts
    status = _task_status(counts, config.expected_packet_count)
    summary = _summary(
        config=config,
        node=node,
        task_id=task_id,
        target_topic=assignment.target_topic,
        schedule_retry_count=assignment.schedule_retry_count,
        mqtt_reconnect_count=mqtt_publisher.reconnect_count,
        mqtt_publish_retry_total=mqtt_publisher.publish_retry_total,
        counts=counts,
        started_ns=started_ns,
        status=status,
        realtime=realtime,
        error_code=_delivery_error_code(status),
    )
    sink.write_task(summary)
    return summary


SenderRunner = Callable[..., dict[str, Any]]


def run_all_senders(
    config: SenderConfig,
    source_files: Mapping[str, Path | str],
    *,
    realtime: bool = True,
    runner: SenderRunner = run_sender_task,
) -> list[dict[str, Any]]:
    expected_sender_ids = {node.sender_id for node in config.senders}
    if set(source_files) != expected_sender_ids:
        raise ValueError("source files must provide exactly one MAT path for every configured sender")

    sink = LocalLogSink(config.log_dir)
    with ThreadPoolExecutor(max_workers=len(config.senders), thread_name_prefix="sender") as executor:
        jobs = [
            (
                node,
                executor.submit(
                    runner,
                    config,
                    node,
                    source_files[node.sender_id],
                    realtime=realtime,
                    log_sink=sink,
                ),
            )
            for node in config.senders
        ]
        summaries: list[dict[str, Any]] = []
        for node, future in jobs:
            try:
                summary = future.result()
            except SenderTaskError as exc:
                summary = exc.summary
            except Exception as exc:
                now = time.time_ns()
                summary = {
                    "device_id": config.device_id,
                    "sender_id": node.sender_id,
                    "bearing_id": node.bearing_id,
                    "task_id": None,
                    "target_topic": None,
                    "expected_packet_count": config.expected_packet_count,
                    "confirmed_packet_count": 0,
                    "failed_packet_count": 0,
                    "dropped_packet_count": 0,
                    "schedule_retry_count": 0,
                    "mqtt_reconnect_count": 0,
                    "mqtt_publish_retry_total": 0,
                    "task_started_timestamp_ns": now,
                    "task_finished_timestamp_ns": now,
                    "task_status": "failed",
                    "replay_mode": "realtime" if realtime else "accelerated",
                    "error_code": "SENDER_TASK_EXCEPTION",
                    "error_message": str(exc),
                }
                sink.write_task(summary)
            summaries.append(summary)
        return summaries
