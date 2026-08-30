from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import paho.mqtt.client as mqtt

from .outbox import ArbitrationOutbox, PermanentDeliveryError
from .repository import SummaryRepository
from .contracts import (
    EXPECTED_BEARING_IDS,
    EXPECTED_EDGE_NODE_IDS,
    InvalidClassProbabilitiesError,
)
from .service import SummaryService
from .suggestion_llm import SuggestionClient
from .suggestions import build_final_suggestion


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummarySettings:
    database_path: Path
    mqtt_host: str
    mqtt_port: int
    mqtt_input_topic: str
    mqtt_output_topic: str
    mqtt_qos: int
    cloud_arbitration_url: str
    cloud_window_sync_url: str
    cloud_timeout_seconds: float
    maintenance_interval_seconds: float
    window_timeout_seconds: float
    mqtt_suggestion_topic: str
    suggestion_llm_enabled: bool
    suggestion_llm_base_url: str
    suggestion_llm_timeout_seconds: float
    suggestion_fallback_text: str
    suggestion_worker_interval_seconds: float
    suggestion_max_attempts: int
    suggestion_retry_delay_seconds: float
    expected_bearing_ids: tuple[str, ...]
    expected_edge_node_ids: tuple[str, ...]


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.getenv(name, ",".join(default)).split(",")
        if item.strip()
    )


def load_summary_settings() -> SummarySettings:
    default_database = Path(__file__).resolve().parents[3] / "data" / "summary_service.db"
    return SummarySettings(
        database_path=Path(os.getenv("SUMMARY_DATABASE_PATH", str(default_database))),
        mqtt_host=os.getenv("SUMMARY_MQTT_HOST", "127.0.0.1"),
        mqtt_port=int(os.getenv("SUMMARY_MQTT_PORT", "1883")),
        mqtt_input_topic=os.getenv(
            "SUMMARY_MQTT_INPUT_TOPIC", "summary/bearing-results"
        ),
        mqtt_output_topic=os.getenv(
            "SUMMARY_MQTT_OUTPUT_TOPIC", "summary/device-results"
        ),
        mqtt_qos=int(os.getenv("SUMMARY_MQTT_QOS", "1")),
        cloud_arbitration_url=os.getenv(
            "SUMMARY_CLOUD_ARBITRATION_URL",
            "http://127.0.0.1:8004/cloud/device-arbitration",
        ),
        cloud_window_sync_url=os.getenv(
            "SUMMARY_CLOUD_WINDOW_SYNC_URL",
            "http://127.0.0.1:8004/cloud/summary-window-results",
        ),
        cloud_timeout_seconds=float(os.getenv("SUMMARY_CLOUD_TIMEOUT_SECONDS", "10")),
        maintenance_interval_seconds=float(
            os.getenv("SUMMARY_MAINTENANCE_INTERVAL_SECONDS", "0.2")
        ),
        window_timeout_seconds=float(os.getenv("SUMMARY_WINDOW_TIMEOUT_SECONDS", "5")),
        mqtt_suggestion_topic=os.getenv(
            "SUMMARY_MQTT_SUGGESTION_TOPIC", "summary/suggestions"
        ),
        suggestion_llm_enabled=(
            os.getenv("SUMMARY_SUGGESTION_LLM_ENABLED", "true").strip().lower()
            == "true"
        ),
        suggestion_llm_base_url=os.getenv(
            "SUMMARY_SUGGESTION_LLM_BASE_URL", "http://127.0.0.1:8005"
        ).rstrip("/"),
        suggestion_llm_timeout_seconds=float(
            os.getenv("SUMMARY_SUGGESTION_LLM_TIMEOUT_SECONDS", "3.0")
        ),
        suggestion_fallback_text=os.getenv(
            "SUMMARY_SUGGESTION_FALLBACK_TEXT", ""
        ).strip(),
        suggestion_worker_interval_seconds=float(
            os.getenv("SUMMARY_SUGGESTION_WORKER_INTERVAL_SECONDS", "0.2")
        ),
        suggestion_max_attempts=int(
            os.getenv("SUMMARY_SUGGESTION_MAX_ATTEMPTS", "5")
        ),
        suggestion_retry_delay_seconds=float(
            os.getenv("SUMMARY_SUGGESTION_RETRY_DELAY_SECONDS", "2.0")
        ),
        expected_bearing_ids=_csv_env(
            "SUMMARY_EXPECTED_BEARING_IDS", EXPECTED_BEARING_IDS
        ),
        expected_edge_node_ids=_csv_env(
            "SUMMARY_EXPECTED_EDGE_NODE_IDS", EXPECTED_EDGE_NODE_IDS
        ),
    )


class SummaryRuntime:
    def __init__(self, settings: SummarySettings) -> None:
        self.settings = settings
        self.repository = SummaryRepository(settings.database_path)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"summary-service-{os.getpid()}",
        )
        self.client.manual_ack_set(True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.suggestion_client = (
            SuggestionClient(
                base_url=settings.suggestion_llm_base_url,
                timeout_seconds=settings.suggestion_llm_timeout_seconds,
            )
            if settings.suggestion_llm_enabled
            else None
        )
        self.service = SummaryService(
            self.repository,
            expected_bearing_ids=settings.expected_bearing_ids,
            expected_edge_node_ids=settings.expected_edge_node_ids,
        )
        self.arbitration_outbox = ArbitrationOutbox(
            self.repository, self._post_arbitration
        )
        self.publish_outbox = ArbitrationOutbox(
            self.repository,
            self._publish_window_result,
            table_name="summary_window_publish_outbox",
        )
        self.window_sync_outbox = ArbitrationOutbox(
            self.repository,
            self._post_window_sync,
            table_name="summary_window_sync_outbox",
        )
        self.suggestion_outbox = ArbitrationOutbox(
            self.repository,
            self._publish_suggestion,
            table_name="summary_suggestion_outbox",
        )
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self._suggestion_worker: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self.client.connect(
            self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30
        )
        self.client.loop_start()
        self._maintenance = threading.Thread(
            target=self._run_maintenance,
            name="summary-maintenance",
            daemon=True,
        )
        self._maintenance.start()
        self._suggestion_worker = threading.Thread(
            target=self._run_suggestion_worker,
            name="summary-suggestion-worker",
            daemon=True,
        )
        self._suggestion_worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._maintenance is not None:
            self._maintenance.join(timeout=5.0)
        if self._suggestion_worker is not None:
            self._suggestion_worker.join(timeout=5.0)
        self.client.disconnect()
        self.client.loop_stop()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.last_error = f"MQTT connect failed: {reason_code}"
            return
        client.subscribe(self.settings.mqtt_input_topic, qos=self.settings.mqtt_qos)
        self._connected.set()
        self.last_error = None

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()

    def _on_message(self, client, userdata, message) -> None:
        payload: Mapping[str, Any] | None = None
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("bearing-result MQTT payload must be an object")
            self.service.ingest(payload)
        except UnicodeDecodeError as exc:
            self._reject_message(
                client,
                message,
                exc,
                metric="rejected_bearing_result_invalid_encoding",
                payload=payload,
            )
        except json.JSONDecodeError as exc:
            self._reject_message(
                client,
                message,
                exc,
                metric="rejected_bearing_result_invalid_json",
                payload=payload,
            )
        except InvalidClassProbabilitiesError as exc:
            self._reject_message(
                client,
                message,
                exc,
                metric="rejected_bearing_result_invalid_probabilities",
                payload=payload,
            )
        except ValueError as exc:
            # Contract-invalid messages are poison records; acknowledge and expose
            # the error instead of redelivering forever.
            self._reject_message(
                client,
                message,
                exc,
                metric="rejected_bearing_result_contract",
                payload=payload,
            )
        except Exception as exc:
            # Leave transient failures unacknowledged; QoS-1 redelivers after a
            # reconnect instead of losing a result that was not committed.
            self.last_error = str(exc)
        else:
            self.last_error = None
            client.ack(message.mid, message.qos)

    def _reject_message(
        self,
        client,
        message,
        error: Exception,
        *,
        metric: str,
        payload: Mapping[str, Any] | None,
    ) -> None:
        try:
            self.repository.increment_metric("rejected_bearing_result_messages")
            self.repository.increment_metric(metric)
        except Exception:
            LOGGER.exception("failed to record rejected bearing-result metric")
        self.last_error = str(error)
        LOGGER.warning(
            "rejected bearing-result message",
            extra={
                "mqtt_mid": getattr(message, "mid", None),
                "mqtt_topic": getattr(message, "topic", None),
                "mqtt_qos": getattr(message, "qos", None),
                "result_id": payload.get("result_id") if payload else None,
                "run_id": payload.get("run_id") if payload else None,
                "summary_window_id": (
                    payload.get("summary_window_id") if payload else None
                ),
                "rejection_metric": metric,
                "error_type": type(error).__name__,
            },
        )
        client.ack(message.mid, message.qos)

    def _publish_window_result(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._publish_mqtt(
            self.settings.mqtt_output_topic, payload, label="window result"
        )

    def _publish_suggestion(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._publish_mqtt(
            self.settings.mqtt_suggestion_topic, payload, label="suggestion"
        )

    def _publish_mqtt(
        self, topic: str, payload: Mapping[str, Any], *, label: str
    ) -> Mapping[str, Any]:
        info = self.client.publish(
            topic,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=self.settings.mqtt_qos,
        )
        info.wait_for_publish(timeout=10.0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS or not info.is_published():
            raise RuntimeError(f"Summary MQTT {label} publish failed: rc={info.rc}")
        return {"published": True}

    def _post_arbitration(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._post_json(self.settings.cloud_arbitration_url, payload)
        self.repository.apply_arbitration_result(
            str(payload["summary_result_id"]), response, now_ns=time.time_ns()
        )
        return response

    def _post_window_sync(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._post_json(self.settings.cloud_window_sync_url, payload)

    def _build_suggestion(self, source: Mapping[str, Any]) -> Mapping[str, Any]:
        return build_final_suggestion(
            source,
            client=self.suggestion_client,
            fallback_override=self.settings.suggestion_fallback_text,
        )

    def process_suggestion_tasks(self, *, limit: int = 5) -> int:
        """Generate due suggestions off the MQTT path; publish via outbox."""

        now = time.time_ns()
        retry_delay_ns = int(
            self.settings.suggestion_retry_delay_seconds * 1_000_000_000
        )
        processed = 0
        for task in self.repository.due_suggestion_tasks(now_ns=now, limit=limit):
            summary_result_id = str(task["summary_result_id"])
            attempts = int(task["attempts"]) + 1
            try:
                suggestion = self._build_suggestion(task["source"])
            except Exception as exc:
                self.repository.defer_suggestion_task(
                    summary_result_id,
                    error=str(exc),
                    attempts=attempts,
                    next_attempt_at_ns=now + retry_delay_ns * attempts,
                    dead_letter=attempts >= self.settings.suggestion_max_attempts,
                    now_ns=now,
                )
            else:
                self.repository.complete_suggestion_task(
                    summary_result_id, suggestion, now_ns=now
                )
                processed += 1
        return processed

    def _post_json(
        self, url: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.cloud_timeout_seconds
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if 400 <= exc.code < 500:
                raise PermanentDeliveryError(
                    f"Cloud rejected request ({exc.code}): {detail}"
                ) from exc
            raise RuntimeError(
                f"Cloud request temporarily failed ({exc.code}): {detail}"
            ) from exc

    def _run_maintenance(self) -> None:
        while not self._stop.wait(self.settings.maintenance_interval_seconds):
            try:
                self.service.close_expired(
                    now_ns=time.time_ns(),
                    timeout_ns=int(self.settings.window_timeout_seconds * 1_000_000_000),
                )
                self.publish_outbox.run_due()
                self.window_sync_outbox.run_due()
                self.arbitration_outbox.run_due()
            except Exception as exc:
                self.last_error = str(exc)

    def _run_suggestion_worker(self) -> None:
        while not self._stop.wait(self.settings.suggestion_worker_interval_seconds):
            try:
                self.process_suggestion_tasks()
                self.suggestion_outbox.run_due()
            except Exception as exc:
                self.last_error = str(exc)
