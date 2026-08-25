from __future__ import annotations

import json
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
from .service import SummaryService


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


def load_summary_settings() -> SummarySettings:
    default_database = Path(__file__).resolve().parents[1] / "data" / "summary_service.db"
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
        self.service = SummaryService(self.repository)
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
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
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

    def stop(self) -> None:
        self._stop.set()
        if self._maintenance is not None:
            self._maintenance.join(timeout=5.0)
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
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("bearing-result MQTT payload must be an object")
            self.service.ingest(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            # Contract-invalid messages are poison records; acknowledge and expose
            # the error instead of redelivering forever.
            self.last_error = str(exc)
            client.ack(message.mid, message.qos)
        except Exception as exc:
            # Leave transient failures unacknowledged; QoS-1 redelivers after a
            # reconnect instead of losing a result that was not committed.
            self.last_error = str(exc)
        else:
            self.last_error = None
            client.ack(message.mid, message.qos)

    def _publish_window_result(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        info = self.client.publish(
            self.settings.mqtt_output_topic,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=self.settings.mqtt_qos,
        )
        info.wait_for_publish(timeout=10.0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS or not info.is_published():
            raise RuntimeError(f"Summary MQTT publish failed: rc={info.rc}")
        return {"published": True}

    def _post_arbitration(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._post_json(self.settings.cloud_arbitration_url, payload)

    def _post_window_sync(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._post_json(self.settings.cloud_window_sync_url, payload)

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
