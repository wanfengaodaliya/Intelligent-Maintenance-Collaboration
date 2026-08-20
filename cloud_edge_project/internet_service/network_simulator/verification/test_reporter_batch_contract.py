"""AUD-02: Reporter batch delivery contract with the real Scheduler batch endpoint."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
import time

from controller.config_loader import load_config
from domain.enums import LinkProtocol, LinkType, NetworkState
from domain.events import ReportCompleted, RejectedLink
from domain.models import LinkSnapshot, RuntimeSnapshot
from plugins.base import PluginContext
from plugins.reporter.client import (
    ReportOutcome,
    ReportSendResult,
    SchedulerReportClient,
)
from plugins.reporter.plugin import ReporterPlugin
from plugins.reporter.schemas import NetworkLinkReport, NetworkReport

from controller.event_bus import EventBus


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
BATCH_URL = "http://scheduler.invalid:8003/scheduler/network-reports"


def _link_report(**overrides) -> NetworkLinkReport:
    fields = {
        "link_id": "sender_01__to__edge_01__mqtt",
        "sender_id": "sender_01",
        "edge_id": "edge_01",
        "protocol": LinkProtocol.MQTT,
        "current_state": NetworkState.GOOD,
        "state_since_ns": 0,
        "state_duration_ms": 1_000,
        "latency_ms": 20,
        "jitter_ms": 5,
        "bandwidth_kbps": 12_000,
        "packet_loss_percent": 1.0,
        "link_reliability_score": 90.0,
        "available": True,
        "last_apply_success": True,
        "last_apply_timestamp_ns": 1,
        "consecutive_apply_failures": 0,
        "error": None,
    }
    fields.update(overrides)
    return NetworkLinkReport(**fields)


def _report(sequence: int = 1, links: int = 18) -> NetworkReport:
    from datetime import datetime, timezone

    return NetworkReport(
        schema_version="1.0",
        experiment_id="exp-test",
        reporter_id="network-controller-01",
        report_sequence=sequence,
        generated_at=datetime.now(tz=timezone.utc),
        generated_at_ns=int(time.time_ns()),
        update_interval_seconds=1.0,
        links=tuple(
            _link_report(link_id=f"link_{index:02d}") for index in range(links)
        ),
    )


class _StubResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StubSession:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.post_calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass


def _client(session: _StubSession) -> SchedulerReportClient:
    return SchedulerReportClient(
        BATCH_URL,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        retry_count=3,
        backoff_base_seconds=0.0,
        session=session,
        wait=lambda seconds: False,
    )


def test_reporter_config_targets_batch_endpoint() -> None:
    config = load_config(CONFIG_DIR, environ={})

    assert config.reporter.scheduler_url.endswith("/scheduler/network-reports")
    # The URL must not be bound to a single link_id anymore.
    assert "__to__" not in config.reporter.scheduler_url


def test_client_accepts_successful_batch_acknowledgement() -> None:
    session = _StubSession(
        [
            _StubResponse(
                200,
                {
                    "accepted": True,
                    "report_sequence": 5,
                    "received_count": 18,
                    "accepted_count": 6,
                    "skipped_count": 12,
                    "rejected_count": 0,
                    "results": [],
                },
            )
        ]
    )

    result = _client(session).send(_report(sequence=5))

    assert result.success is True
    assert result.status_code == 200
    assert result.accepted_count == 6
    assert result.rejected_count == 0
    assert result.rejected_links == ()
    assert len(session.post_calls) == 1


def test_client_treats_partial_rejection_as_failure_without_retry() -> None:
    session = _StubSession(
        [
            _StubResponse(
                200,
                {
                    "accepted": False,
                    "report_sequence": 6,
                    "received_count": 18,
                    "accepted_count": 17,
                    "skipped_count": 0,
                    "rejected_count": 1,
                    "results": [
                        {
                            "link_id": "sender_01__to__edge_99__mqtt",
                            "accepted": False,
                            "reason": "unregistered_edge_node",
                        }
                    ],
                },
            )
        ]
    )

    result = _client(session).send(_report(sequence=6))

    assert result.success is False
    # NET-1: 部分接受（17/18）必须分类为 PARTIAL_SUCCESS，不得当作整批 dropped。
    assert result.outcome is ReportOutcome.PARTIAL_SUCCESS
    assert result.status_code == 200
    assert result.accepted_count == 17
    assert result.rejected_count == 1
    assert result.rejected_links == (
        RejectedLink(
            link_id="sender_01__to__edge_99__mqtt",
            reason="unregistered_edge_node",
        ),
    )
    assert "sender_01__to__edge_99__mqtt" in (result.error or "")
    assert "unregistered_edge_node" in (result.error or "")
    # A deterministic semantic rejection must not be retried.
    assert result.retry_count == 0
    assert len(session.post_calls) == 1


def test_client_treats_full_rejection_as_total_failure_without_retry() -> None:
    """NET-1：accepted_count=0 且存在应处理链路 → TOTAL_FAILURE。"""
    session = _StubSession(
        [
            _StubResponse(
                200,
                {
                    "accepted": False,
                    "report_sequence": 6,
                    "received_count": 18,
                    "accepted_count": 0,
                    "skipped_count": 0,
                    "rejected_count": 6,
                    "results": [],
                },
            )
        ]
    )

    result = _client(session).send(_report(sequence=6))

    assert result.success is False
    assert result.outcome is ReportOutcome.TOTAL_FAILURE
    assert result.status_code == 200
    assert result.accepted_count == 0
    assert result.rejected_count == 6
    assert len(session.post_calls) == 1


def test_client_accepts_legacy_acknowledgement() -> None:
    """Old single-link acknowledgements (fake scheduler) still validate."""
    session = _StubSession(
        [_StubResponse(200, {"accepted": True, "report_sequence": 7})]
    )

    result = _client(session).send(_report(sequence=7))

    assert result.success is True
    assert result.accepted_count is None
    assert result.rejected_count is None


def test_plugin_degrades_health_and_publishes_rejected_links(caplog) -> None:
    config = load_config(CONFIG_DIR, environ={})
    rejected = (
        RejectedLink(
            link_id="sender_01__to__edge_99__mqtt",
            reason="unregistered_edge_node",
        ),
    )

    class _RejectedOnceClient:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, report: NetworkReport) -> ReportSendResult:
            self.calls += 1
            return ReportSendResult(
                report_sequence=report.report_sequence,
                success=False,
                status_code=200,
                retry_count=0,
                duration_ms=1.0,
                error="Scheduler rejected 1 link(s)",
                accepted_count=17,
                rejected_count=1,
                rejected_links=rejected,
                outcome=ReportOutcome.PARTIAL_SUCCESS,
            )

        def close(self) -> None:
            pass

    stub_client = _RejectedOnceClient()
    event_bus = EventBus()
    published: list[object] = []
    event_bus.subscribe(ReportCompleted, published.append)
    context = PluginContext(
        config=config,
        runtime_store=None,
        event_bus=event_bus,
        shutdown_event=Event(),
    )
    plugin = ReporterPlugin(client_factory=lambda _: stub_client)
    plugin.initialize(context)
    plugin.start()
    try:
        link = LinkSnapshot(
            link_id="sender_01__to__edge_01__mqtt",
            link_type=LinkType.SENDER_TO_EDGE,
            sender_id="sender_01",
            edge_id="edge_01",
            protocol=LinkProtocol.MQTT,
            proxy_name="sender_01__to__edge_01__mqtt",
            listen="0.0.0.0:18831",
            advertised_host="toxiproxy",
            advertised_port=18831,
            upstream="mqtt-broker:1883",
            current_state=NetworkState.GOOD,
            previous_state=NetworkState.GOOD,
            state_since_ns=0,
            seed=1,
            applied_state_since_ns=None,
            desired_parameters=None,
            applied_parameters=None,
            link_reliability_score=90.0,
            score_components={},
            available=True,
            last_apply_success=True,
            last_apply_timestamp_ns=1,
            consecutive_apply_failures=0,
            last_error=None,
            report_enabled=True,
        )
        snapshot = RuntimeSnapshot(
            tick=1,
            generated_at_ns=time.time_ns(),
            links=(link,),
        )

        with caplog.at_level("WARNING", logger="network_simulator.plugins.reporter"):
            assert plugin.submit(snapshot) is not None
            deadline = time.monotonic() + 5.0
            while plugin.health()["last_report_success"] is None:
                if time.monotonic() > deadline:
                    raise AssertionError("reporter worker did not process the report")
                time.sleep(0.01)

        health = plugin.health()
        assert health["status"] == "degraded"
        assert health["last_report_success"] is False
        assert health["last_outcome"] == ReportOutcome.PARTIAL_SUCCESS
        assert health["last_rejected_count"] == 1
        assert health["last_rejected_links"] == [
            {"link_id": "sender_01__to__edge_99__mqtt", "reason": "unregistered_edge_node"}
        ]
        # NET-1：部分成功不得计为整份报告 dropped，也不得计入 total 连续失败。
        assert health["dropped_report_count"] == 0
        assert health["consecutive_failures"] == 0
        assert health["partial_failure_count"] == 1
        assert stub_client.calls == 1

        assert len(published) == 1
        event = published[0]
        assert isinstance(event, ReportCompleted)
        assert event.success is False
        assert event.rejected_count == 1
        assert event.rejected_links == rejected

        warning_texts = [
            record.getMessage() for record in caplog.records if record.levelno >= 30
        ]
        assert any("sender_01__to__edge_99__mqtt" in text for text in warning_texts)
    finally:
        plugin.stop()


def _snapshot_with_one_link() -> RuntimeSnapshot:
    link = LinkSnapshot(
        link_id="sender_01__to__edge_01__mqtt",
        link_type=LinkType.SENDER_TO_EDGE,
        sender_id="sender_01",
        edge_id="edge_01",
        protocol=LinkProtocol.MQTT,
        proxy_name="sender_01__to__edge_01__mqtt",
        listen="0.0.0.0:18831",
        advertised_host="toxiproxy",
        advertised_port=18831,
        upstream="mqtt-broker:1883",
        current_state=NetworkState.GOOD,
        previous_state=NetworkState.GOOD,
        state_since_ns=0,
        seed=1,
        applied_state_since_ns=None,
        desired_parameters=None,
        applied_parameters=None,
        link_reliability_score=90.0,
        score_components={},
        available=True,
        last_apply_success=True,
        last_apply_timestamp_ns=1,
        consecutive_apply_failures=0,
        last_error=None,
        report_enabled=True,
    )
    return RuntimeSnapshot(tick=1, generated_at_ns=time.time_ns(), links=(link,))


class _ScriptedClient:
    """Returns scripted success/failure outcomes per send call."""

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def send(self, report: NetworkReport) -> ReportSendResult:
        self.calls += 1
        success = self._outcomes.pop(0) if self._outcomes else True
        return ReportSendResult(
            report_sequence=report.report_sequence,
            success=success,
            status_code=200 if success else None,
            retry_count=0,
            duration_ms=1.0,
            error=None if success else "ConnectionError: Scheduler request failed",
        )

    def close(self) -> None:
        pass


def _wait_for_reporter(plugin: ReporterPlugin, expected_failures: int) -> None:
    deadline = time.monotonic() + 5.0
    while plugin.health()["consecutive_failures"] != expected_failures:
        if time.monotonic() > deadline:
            raise AssertionError(
                "reporter worker did not reach expected failure count "
                f"{expected_failures}"
            )
        time.sleep(0.01)


def test_plugin_logs_first_failure_and_recovery_with_health_fields(caplog) -> None:
    """AUD-13: failures and recovery must be observable in logs and health."""
    config = load_config(CONFIG_DIR, environ={})
    stub_client = _ScriptedClient([False, False, True])
    event_bus = EventBus()
    context = PluginContext(
        config=config,
        runtime_store=None,
        event_bus=event_bus,
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    plugin = ReporterPlugin(
        client_factory=lambda _: stub_client,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: (
            clock.__setitem__("mono_ns", clock["mono_ns"] + 10_000_000_000)
            or clock["mono_ns"]
        ),
    )
    plugin.initialize(context)
    plugin.start()
    try:
        snapshot = _snapshot_with_one_link()
        with caplog.at_level(
            logging.INFO, logger="network_simulator.plugins.reporter"
        ):
            assert plugin.submit(snapshot) is not None
            _wait_for_reporter(plugin, 1)

            clock["now_ns"] += 5_000_000_000
            assert plugin.submit(snapshot) is not None
            _wait_for_reporter(plugin, 2)

            clock["now_ns"] += 5_000_000_000
            assert plugin.submit(snapshot) is not None
            _wait_for_reporter(plugin, 0)

        health = plugin.health()
        assert health["last_report_success"] is True
        assert health["last_success_ns"] == 1_700_000_010_000_000_000
        assert health["last_failure_ns"] == 1_700_000_005_000_000_000
        assert health["consecutive_failures"] == 0

        failure_logs = [
            record.getMessage()
            for record in caplog.records
            if "Scheduler report failed" in record.getMessage()
        ]
        # Failure #2 is suppressed by the rate limiter.
        assert len(failure_logs) == 1
        assert "consecutive_failures=1" in failure_logs[0]
        assert "Scheduler request failed" in failure_logs[0]

        recovery_logs = [
            record.getMessage()
            for record in caplog.records
            if "Scheduler reporter recovered" in record.getMessage()
        ]
        assert len(recovery_logs) == 1
        assert "previous_failures=2" in recovery_logs[0]
        assert "failed_duration_seconds=10.0" in recovery_logs[0]
    finally:
        plugin.stop()


def test_plugin_failure_logging_is_rate_limited(caplog) -> None:
    """AUD-13: persistent failures log at 1st/3rd/10th/every 30th only."""
    config = load_config(CONFIG_DIR, environ={})
    stub_client = _ScriptedClient([False] * 12)
    event_bus = EventBus()
    context = PluginContext(
        config=config,
        runtime_store=None,
        event_bus=event_bus,
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    plugin = ReporterPlugin(
        client_factory=lambda _: stub_client,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: (
            clock.__setitem__("mono_ns", clock["mono_ns"] + 10_000_000_000)
            or clock["mono_ns"]
        ),
    )
    plugin.initialize(context)
    plugin.start()
    try:
        snapshot = _snapshot_with_one_link()
        with caplog.at_level(
            logging.WARNING, logger="network_simulator.plugins.reporter"
        ):
            for expected in range(1, 13):
                assert plugin.submit(snapshot) is not None
                _wait_for_reporter(plugin, expected)

        failure_logs = [
            record.getMessage()
            for record in caplog.records
            if "Scheduler report failed" in record.getMessage()
        ]
        assert len(failure_logs) == 3
        assert "consecutive_failures=1" in failure_logs[0]
        assert "consecutive_failures=3" in failure_logs[1]
        assert "consecutive_failures=10" in failure_logs[2]

        health = plugin.health()
        assert health["status"] == "degraded"
        assert health["consecutive_failures"] == 12
        assert health["last_success_ns"] is None
    finally:
        plugin.stop()


def test_health_plugin_exposes_reporter_observability_fields() -> None:
    """AUD-13: /health must surface reporter failure counters for debugging."""
    from types import SimpleNamespace

    from plugins.api.schemas import HealthResponse
    from plugins.health.plugin import HealthPlugin

    config = load_config(CONFIG_DIR, environ={})
    context = PluginContext(
        config=config,
        runtime_store=SimpleNamespace(
            snapshot=lambda: RuntimeSnapshot(
                tick=1, generated_at_ns=1, links=()
            )
        ),
        event_bus=EventBus(),
        shutdown_event=Event(),
    )
    reporter_health = {
        "status": "degraded",
        "queue_size": 0,
        "dropped_count": 0,
        "last_report_success": False,
        "last_error": "ConnectionError: Scheduler request failed",
        "last_accepted_count": 17,
        "last_rejected_count": 1,
        "last_rejected_links": [],
        "last_success_ns": 111,
        "last_failure_ns": 222,
        "consecutive_failures": 3,
    }
    plugin = HealthPlugin(
        toxiproxy_health=lambda: {"status": "ok"},
        reporter_health=lambda: reporter_health,
    )
    plugin.initialize(context)
    plugin.start()

    health = plugin.health()

    assert health["status"] == "degraded"
    assert health["scheduler_reporter_healthy"] is False
    assert health["scheduler_reporter"] == {
        "status": "degraded",
        "last_error": "ConnectionError: Scheduler request failed",
        "last_success_ns": 111,
        "last_failure_ns": 222,
        "consecutive_failures": 3,
        "last_rejected_count": 1,
        "last_outcome": None,
        "partial_failure_count": None,
    }
    # The API schema (extra=forbid) must accept the extended health payload.
    response = HealthResponse.model_validate(dict(health))
    assert response.scheduler_reporter is not None
    assert response.scheduler_reporter.consecutive_failures == 3
    assert response.scheduler_reporter.last_error == (
        "ConnectionError: Scheduler request failed"
    )


def test_net3_final_delivery_failures_increment_dropped_report_count() -> None:
    """NET-3：最终交付失败累计 dropped_report_count，恢复后计数保留。

    3 次最终失败 → dropped_report_count=3；随后成功 → consecutive_failures=0，
    但 dropped_report_count 仍为 3（累计历史，不因恢复而清零）。
    """
    config = load_config(CONFIG_DIR, environ={})
    # 3 次最终失败 + 1 次成功。
    stub_client = _ScriptedClient([False, False, False, True])
    event_bus = EventBus()
    context = PluginContext(
        config=config,
        runtime_store=None,
        event_bus=event_bus,
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    plugin = ReporterPlugin(
        client_factory=lambda _: stub_client,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: (
            clock.__setitem__("mono_ns", clock["mono_ns"] + 10_000_000_000)
            or clock["mono_ns"]
        ),
    )
    plugin.initialize(context)
    plugin.start()
    try:
        snapshot = _snapshot_with_one_link()
        # 3 次最终发送失败。
        for expected in (1, 2, 3):
            assert plugin.submit(snapshot) is not None
            _wait_for_reporter(plugin, expected)

        health = plugin.health()
        assert health["dropped_report_count"] == 3
        assert health["consecutive_failures"] == 3
        assert health["last_report_success"] is False

        # 恢复：成功交付。
        clock["now_ns"] += 5_000_000_000
        assert plugin.submit(snapshot) is not None
        _wait_for_reporter(plugin, 0)

        health = plugin.health()
        assert health["last_report_success"] is True
        assert health["consecutive_failures"] == 0
        # 累计历史保留。
        assert health["dropped_report_count"] == 3
    finally:
        plugin.stop()
