from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from sender.config import SenderConfig, SenderNodeConfig
from sender.input_adapter import (
    SenderInputAdapter,
    SenderInputAdapterProvider,
)
from sender.ids import TaskIdStore
from sender.local_logs import LocalLogSink
from sender.mqtt_publisher import MqttPublisher
from sender.scheduler_client import SchedulerClient, SchedulerError
from bootstrap.scenarios import build_sender_scenario_registry
from core.scenario_plugin import INPUT_ADAPTER
from compatibility.bearing_v12.scenario_mapper import BEARING_SCENARIO_TYPE


scenario_registry = build_sender_scenario_registry()
input_adapter_provider = cast(
    SenderInputAdapterProvider,
    scenario_registry.require_provider(
        BEARING_SCENARIO_TYPE,
        INPUT_ADAPTER,
    ),
)


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


def resolve_target_edge_node_id(target_topic: str) -> str | None:
    """Extract the edge node id from `edge/{edge_node_id}/input` topics."""
    parts = target_topic.split("/")
    if len(parts) == 3 and parts[0] == "edge" and parts[1] and parts[2] == "input":
        return parts[1]
    return None


def _mqtt_port_for_target(
    node: SenderNodeConfig, target_topic: str
) -> tuple[int, str | None]:
    """Choose the per-link proxy port when the target edge has a mapped port."""
    target_edge = resolve_target_edge_node_id(target_topic)
    if target_edge is not None and target_edge in node.edge_mqtt_proxy_ports:
        return node.edge_mqtt_proxy_ports[target_edge], target_edge
    return node.mqtt_port, target_edge


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
    target_edge_node_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "device_id": config.device_id,
        "sender_id": node.sender_id,
        "bearing_id": node.bearing_id,
        "task_id": task_id,
        "target_topic": target_topic,
        "target_edge_node_id": target_edge_node_id,
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
    source_mapping_store: object | None = None,
    input_adapter: SenderInputAdapter | None = None,
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
    adapter = input_adapter or input_adapter_provider.build_adapter(
        config.state_dir,
        source_mapping_store,
    )
    started_ns = time.time_ns()
    prepared_input = adapter.prepare(
        source_path,
        unit_id=node.unit_id,
        duration_ms=config.packet_interval_ms,
        count=config.expected_packet_count,
    )
    first_window = prepared_input.first_window

    preview_packet = adapter.build_packet(
        device_id=config.device_id,
        task_id=task_id,
        unit_id=node.unit_id,
        sender_id=node.sender_id,
        sequence_number=first_window.sequence_number,
        window=first_window,
        end_generate_timestamp_ns=started_ns,
    )
    schedule_request = {
        "device_id": config.device_id,
        "sender_id": node.sender_id,
        "task_id": task_id,
        "bearing_id": node.bearing_id,
        "packet_size_bytes": len(adapter.serialize_packet(preview_packet)),
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

    # 阶段 4：按 Scheduler 分配的目标 Edge 选择对应网络模拟代理端口，
    # 使 Sender→Edge 流量经过 per-link 的 Toxiproxy 链路。
    mqtt_port, target_edge_node_id = _mqtt_port_for_target(node, assignment.target_topic)
    mqtt_publisher = publisher or MqttPublisher(
        sender_id=node.sender_id,
        host=node.mqtt_host,
        port=mqtt_port,
        keepalive_seconds=config.mqtt_keepalive_seconds,
        qos=config.qos,
        retain=config.retain,
        warning_timeout_ms=config.puback_warning_timeout_ms,
        delivery_timeout_ms=config.packet_delivery_timeout_ms,
        max_retries=config.max_publish_retries,
        queue_max_packets=config.pending_queue_max_packets,
        recovery_retry_interval_ms=config.packet_interval_ms,
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
            window = adapter.next_window(
                prepared_input,
                unit_id=node.unit_id,
                expected_sequence=sequence_number,
            )
            packet = adapter.build_packet(
                device_id=config.device_id,
                task_id=task_id,
                unit_id=node.unit_id,
                sender_id=node.sender_id,
                sequence_number=sequence_number,
                window=window,
                end_generate_timestamp_ns=(
                    started_ns
                    + (sequence_number - 1) * config.packet_interval_ms * 1_000_000
                ),
            )
            adapter.persist_source(
                packet=packet,
                task_id=task_id,
                unit_id=node.unit_id,
                source_path=prepared_input.window_source_paths.get(
                    sequence_number,
                    prepared_input.source_path,
                ),
                window=window,
            )
            mqtt_publisher.publish(
                packet,
                adapter.serialize_packet(packet),
                assignment.target_topic,
            )

        mqtt_publisher.wait_until_settled(config.recovery_window_seconds)
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
            target_edge_node_id=target_edge_node_id,
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
        target_edge_node_id=target_edge_node_id,
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
        raise ValueError(
            "source paths must provide exactly one MAT file or directory "
            "for every configured sender"
        )

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
