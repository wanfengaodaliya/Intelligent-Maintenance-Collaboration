"""NET-1: FULL/PARTIAL/TOTAL delivery semantics for the Network Reporter.

部分成功（部分链路被接受 + 部分被拒绝）必须不再被当作“整份报告 dropped”，
也不得计入 total-delivery 连续失败；真正完全失败与 transport 失败仍必须正确
degraded/failed。T6 联合 ENV-1 验证默认 18 条链路（6 MQTT + 12 HTTP skipped）。
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
import time

from controller.config_loader import load_config
from domain.enums import LinkProtocol, LinkType, NetworkState
from domain.models import LinkSnapshot, RuntimeSnapshot
from plugins.base import PluginContext
from plugins.reporter.client import ReportOutcome, ReportSendResult
from plugins.reporter.plugin import ReporterPlugin
from plugins.reporter.schemas import NetworkReport

from controller.event_bus import EventBus


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _snapshot() -> RuntimeSnapshot:
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


def _wait(plugin: ReporterPlugin, *, field: str, value: object) -> None:
    deadline = time.monotonic() + 5.0
    while plugin.health()[field] != value:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"reporter did not reach {field}={value!r}, got "
                f"{plugin.health()[field]!r}"
            )
        time.sleep(0.01)


# T1: 全部接受 → FULL_SUCCESS，consecutive=0，dropped=0。
def test_full_success_is_clean() -> None:
    config = load_config(CONFIG_DIR, environ={})
    context = PluginContext(
        config=config, runtime_store=None, event_bus=EventBus(),
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    class _FullOkClient:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, report: NetworkReport) -> ReportSendResult:
            self.calls += 1
            return ReportSendResult(
                report_sequence=report.report_sequence,
                success=True,
                status_code=200,
                retry_count=0,
                duration_ms=1.0,
                error=None,
                accepted_count=6,
                rejected_count=0,
                outcome=ReportOutcome.FULL_SUCCESS,
            )

        def close(self) -> None:
            pass

    stub = _FullOkClient()
    plugin = ReporterPlugin(
        client_factory=lambda _: stub,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: clock["mono_ns"],
    )
    plugin.initialize(context)
    plugin.start()
    try:
        assert plugin.submit(_snapshot()) is not None
        _wait(plugin, field="last_report_success", value=True)
        health = plugin.health()
        assert health["status"] == "ok"
        assert health["last_outcome"] == ReportOutcome.FULL_SUCCESS
        assert health["consecutive_failures"] == 0
        assert health["dropped_report_count"] == 0
        assert health["partial_failure_count"] == 0
    finally:
        plugin.stop()


# T2: 部分成功（received=4, accepted=2, skipped=1, rejected=1）→ 不占 dropped，
#     不使用 total 连续失败，partial 可见。
def test_partial_success_is_observable_but_not_dropped() -> None:
    config = load_config(CONFIG_DIR, environ={})
    context = PluginContext(
        config=config, runtime_store=None, event_bus=EventBus(),
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    class _PartialClient:
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
                accepted_count=2,
                rejected_count=1,
                outcome=ReportOutcome.PARTIAL_SUCCESS,
            )

        def close(self) -> None:
            pass

    stub = _PartialClient()
    plugin = ReporterPlugin(
        client_factory=lambda _: stub,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: clock["mono_ns"],
    )
    plugin.initialize(context)
    plugin.start()
    try:
        assert plugin.submit(_snapshot()) is not None
        _wait(plugin, field="partial_failure_count", value=1)
        health = plugin.health()
        assert health["last_outcome"] == ReportOutcome.PARTIAL_SUCCESS
        assert health["dropped_report_count"] == 0
        assert health["consecutive_failures"] == 0
        assert health["partial_failure_count"] == 1
        assert health["last_rejected_count"] == 1
    finally:
        plugin.stop()


# T3: 全部失败（accepted=0, rejected>0）→ dropped+1, consecutive+1。
def test_total_rejection_counts_as_dropped() -> None:
    config = load_config(CONFIG_DIR, environ={})
    context = PluginContext(
        config=config, runtime_store=None, event_bus=EventBus(),
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    class _TotalClient:
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
                error="Scheduler rejected 6 link(s)",
                accepted_count=0,
                rejected_count=6,
                outcome=ReportOutcome.TOTAL_FAILURE,
            )

        def close(self) -> None:
            pass

    stub = _TotalClient()
    plugin = ReporterPlugin(
        client_factory=lambda _: stub,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: clock["mono_ns"],
    )
    plugin.initialize(context)
    plugin.start()
    try:
        assert plugin.submit(_snapshot()) is not None
        _wait(plugin, field="consecutive_failures", value=1)
        health = plugin.health()
        assert health["last_outcome"] == ReportOutcome.TOTAL_FAILURE
        assert health["dropped_report_count"] == 1
        assert health["consecutive_failures"] == 1
    finally:
        plugin.stop()


# T4: transport 失败（ConnectionError）→ 仍 dropped+1, consecutive+1。
def test_transport_failure_still_counts_as_dropped(caplog) -> None:
    config = load_config(CONFIG_DIR, environ={})
    context = PluginContext(
        config=config, runtime_store=None, event_bus=EventBus(),
        shutdown_event=Event(),
    )
    clock = {"now_ns": 1_700_000_000_000_000_000, "mono_ns": 0}

    class _ConnErrorClient:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, report: NetworkReport) -> ReportSendResult:
            self.calls += 1
            return ReportSendResult(
                report_sequence=report.report_sequence,
                success=False,
                status_code=None,
                retry_count=3,
                duration_ms=120.0,
                error="ConnectionError: Scheduler request failed",
                outcome=ReportOutcome.TOTAL_FAILURE,
            )

        def close(self) -> None:
            pass

    stub = _ConnErrorClient()
    plugin = ReporterPlugin(
        client_factory=lambda _: stub,
        now_ns=lambda: clock["now_ns"],
        monotonic_ns=lambda: clock["mono_ns"],
    )
    plugin.initialize(context)
    plugin.start()
    try:
        assert plugin.submit(_snapshot()) is not None
        _wait(plugin, field="consecutive_failures", value=1)
        health = plugin.health()
        assert health["status"] == "degraded"
        assert health["last_outcome"] == ReportOutcome.TOTAL_FAILURE
        assert health["dropped_report_count"] == 1
        assert health["consecutive_failures"] == 1
    finally:
        plugin.stop()


# T5 / T6: skipped-only 与默认完整拓扑（6 MQTT 接受 + 12 HTTP 跳过）都必须是 FULL_SUCCESS，
#         不因 skipped 而降级，不增加 dropped/consecutive。

class _StubResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StubSession:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls = 0

    def post(self, url: str, **kwargs):
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def close(self) -> None:
        pass


def _real_client(session: _StubSession):
    from plugins.reporter.client import SchedulerReportClient

    return SchedulerReportClient(
        "http://scheduler.invalid:8003/scheduler/network-reports",
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        retry_count=2,
        backoff_base_seconds=0.0,
        session=session,
        wait=lambda seconds: False,
    )


def _report(sequence: int = 1) -> NetworkReport:
    from datetime import datetime, timezone

    return NetworkReport(
        schema_version="1.0",
        experiment_id="exp-test",
        reporter_id="network-controller-01",
        report_sequence=sequence,
        generated_at=datetime.now(tz=timezone.utc),
        generated_at_ns=int(time.time_ns()),
        update_interval_seconds=1.0,
        links=(),
    )


def test_default_18_link_topology_is_full_success() -> None:
    """T6（结合 ENV-1）：默认 6 MQTT accepted + 12 HTTP skipped → FULL_SUCCESS。"""
    session = _StubSession(
        _StubResponse(
            200,
            {
                "accepted": True,
                "report_sequence": 20,
                "received_count": 18,
                "accepted_count": 6,
                "skipped_count": 12,
                "rejected_count": 0,
                "results": [],
            },
        )
    )
    result = _real_client(session).send(_report(sequence=20))
    assert result.success is True
    assert result.outcome is ReportOutcome.FULL_SUCCESS
    assert result.accepted_count == 6
    assert result.rejected_count == 0
    assert result.rejected_links == ()


def test_skipped_only_links_are_not_a_failure() -> None:
    """T5：skipped 属于设计边界，accepted=True + skipped>0 + rejected=0 → FULL_SUCCESS。"""
    session = _StubSession(
        _StubResponse(
            200,
            {
                "accepted": True,
                "report_sequence": 21,
                "received_count": 18,
                "accepted_count": 2,
                "skipped_count": 16,
                "rejected_count": 0,
                "results": [],
            },
        )
    )
    result = _real_client(session).send(_report(sequence=21))
    assert result.success is True
    assert result.outcome is ReportOutcome.FULL_SUCCESS
    assert result.rejected_count == 0
    assert result.accepted_count == 2