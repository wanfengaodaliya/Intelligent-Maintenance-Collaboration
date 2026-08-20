"""NET-1: Toxiproxy restart self-healing for a missing proxy (HTTP 404).

Toxiproxy 容器重启后内存中的 proxy/toxic 全部消失，但 Controller 不重启。
自愈契约：
* 仅当明确 proxy-not-found（ProxyNotFoundError / HTTP 404）才触发 ensure_proxy；
* 最多 apply 两次（一次失败 + 一次自愈后重试），禁止无限重试；
* ProxyConflictError / 不可用等不当作“缺失”误重建，直接记录失败；
* 正常路径无额外 API 开销。
"""

from __future__ import annotations

from threading import Event, Lock
from typing import Any

from controller.config_loader import ResolvedLinkConfig
from controller.runtime_store import RuntimeStore
from domain.enums import DisconnectMode, LinkProtocol, LinkType, NetworkState
from domain.exceptions import (
    ProxyConflictError,
    ProxyNotFoundError,
    ToxicOperationError,
)
from domain.models import ApplyResult, LinkRuntime, NetworkParameters
from plugins.base import PluginContext
from plugins.toxiproxy.plugin import ToxiproxyPlugin


def _desired() -> NetworkParameters:
    return NetworkParameters(
        state=NetworkState.GOOD,
        latency_ms=20,
        jitter_ms=5,
        bandwidth_kbps=12_000,
        packet_loss_percent=0.0,
        disconnect_mode=DisconnectMode.NONE,
    )


def _link_config(link_id: str = "link_01") -> ResolvedLinkConfig:
    return ResolvedLinkConfig(
        link_id=link_id,
        link_type=LinkType.SENDER_TO_EDGE,
        sender_id="sender_01",
        edge_id="edge_01",
        protocol=LinkProtocol.MQTT,
        proxy_name=f"{link_id}_proxy",
        listen="0.0.0.0:22222",
        advertised_host="edge_01",
        advertised_port=22222,
        upstream="edge_01:1883",
        seed_offset=None,
        latency_stream="upstream",
        bandwidth_stream="downstream",
        disconnect_stream="downstream",
        disconnect_mode="auto",
        report_enabled=False,
    )


class _FakeApplier:
    def __init__(self, outcomes: list[ApplyResult | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    def apply(
        self,
        link: Any,
        desired: NetworkParameters,
        previous_applied: NetworkParameters | None,
        timestamp_ns: int,
    ) -> ApplyResult:
        self.calls.append((link.link_id, timestamp_ns))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, ensure_outcomes: list[Exception | None] | None = None) -> None:
        self._ensure_outcomes = list(ensure_outcomes or [])
        self.ensure_calls: list[tuple[str, str, str]] = []

    def ensure_proxy(self, name: str, listen: str, upstream: str) -> None:
        self.ensure_calls.append((name, listen, upstream))
        if self._ensure_outcomes:
            outcome = self._ensure_outcomes.pop(0)
            if outcome is not None:
                raise outcome


def _success(link_id: str, timestamp_ns: int) -> ApplyResult:
    return ApplyResult(
        link_id=link_id,
        success=True,
        applied_parameters=_desired(),
        timestamp_ns=timestamp_ns,
        error=None,
        packet_loss_applied=False,
    )


def _failure(link_id: str, timestamp_ns: int, error: str) -> ApplyResult:
    return ApplyResult(
        link_id=link_id,
        success=False,
        applied_parameters=None,
        timestamp_ns=timestamp_ns,
        error=error,
        packet_loss_applied=False,
    )


def _build_plugin(
    applier: _FakeApplier,
    client: _FakeClient,
    *,
    link_id: str = "link_01",
) -> tuple[ToxiproxyPlugin, RuntimeStore]:
    link = _link_config(link_id)
    runtime = LinkRuntime(
        link_id=link.link_id,
        link_type=link.link_type,
        sender_id=link.sender_id,
        edge_id=link.edge_id,
        protocol=link.protocol,
        proxy_name=link.proxy_name,
        listen=link.listen,
        advertised_host=link.advertised_host,
        advertised_port=link.advertised_port,
        upstream=link.upstream,
        current_state=NetworkState.GOOD,
        previous_state=NetworkState.GOOD,
        state_since_ns=0,
        seed=1,
        desired_parameters=_desired(),
    )
    store = RuntimeStore([runtime])
    plugin = ToxiproxyPlugin(client, applier=applier)
    config = PluginConfigStub(links=(link,))
    plugin._context = PluginContext(
        config=config,
        runtime_store=store,
        event_bus=None,
        shutdown_event=Event(),
    )
    plugin._links = {link.link_id: link}
    plugin._link_locks = {link.link_id: Lock()}
    return plugin, store


class PluginConfigStub:
    def __init__(self, links: tuple[ResolvedLinkConfig, ...]) -> None:
        self.links = links


def test_t1_missing_proxy_recovers_with_single_reconcile() -> None:
    timestamp_ns = 1_000
    outcomes: list[ApplyResult | Exception] = [
        ProxyNotFoundError("proxy link_01_proxy not found"),
        _success("link_01", timestamp_ns),
    ]
    client = _FakeClient()
    plugin, store = _build_plugin(_FakeApplier(outcomes), client)

    result = plugin._apply_and_store("link_01", generation=0, timestamp_ns=timestamp_ns)

    assert result.success is True
    assert [call[0] for call in plugin._applier.calls] == ["link_01", "link_01"]
    assert len(client.ensure_calls) == 1
    name, listen, upstream = client.ensure_calls[0]
    assert name == "link_01_proxy"
    assert listen == "0.0.0.0:22222"
    assert upstream == "edge_01:1883"
    assert store.get_link("link_01").last_apply_success is True


def test_t2_reconcile_retry_still_fails_no_infinite_loop() -> None:
    timestamp_ns = 2_000
    outcomes: list[ApplyResult | Exception] = [
        ProxyNotFoundError("link_01_proxy not found"),
        _failure("link_01", timestamp_ns, "ToxicOperationError: still missing"),
    ]
    client = _FakeClient()
    plugin, store = _build_plugin(_FakeApplier(outcomes), client)

    result = plugin._apply_and_store("link_01", generation=0, timestamp_ns=timestamp_ns)

    assert result.success is False
    assert len(plugin._applier.calls) == 2
    assert len(client.ensure_calls) == 1
    snapshot = store.get_link("link_01")
    assert snapshot.last_apply_success is False
    assert "still missing" in (snapshot.last_error or "")


def test_t3_ensure_proxy_conflict_is_not_swallowed() -> None:
    timestamp_ns = 3_000
    outcomes: list[ApplyResult | Exception] = [
        ProxyNotFoundError("link_01_proxy not found"),
    ]
    client = _FakeClient(ensure_outcomes=[ProxyConflictError("port conflict")])
    plugin, store = _build_plugin(_FakeApplier(outcomes), client)

    result = plugin._apply_and_store("link_01", generation=0, timestamp_ns=timestamp_ns)

    assert result.success is False
    assert len(plugin._applier.calls) == 1  # conflict prevents the retry apply
    assert len(client.ensure_calls) == 1
    assert "ProxyConflictError" in (result.error or "")
    assert store.get_link("link_01").last_apply_success is False


def test_t4_normal_path_has_no_reconcile_overhead() -> None:
    timestamp_ns = 4_000
    outcomes: list[ApplyResult | Exception] = [_success("link_01", timestamp_ns)]
    client = _FakeClient()
    plugin, store = _build_plugin(_FakeApplier(outcomes), client)

    result = plugin._apply_and_store("link_01", generation=0, timestamp_ns=timestamp_ns)

    assert result.success is True
    assert len(plugin._applier.calls) == 1
    assert len(client.ensure_calls) == 0
    assert store.get_link("link_01").last_apply_success is True


def test_t5_non_proxy_missing_errors_do_not_trigger_reconcile() -> None:
    timestamp_ns = 5_000
    outcomes: list[ApplyResult | Exception] = [
        ToxicOperationError("invalid toxic attributes"),
    ]
    client = _FakeClient()
    plugin, store = _build_plugin(_FakeApplier(outcomes), client)

    result = plugin._apply_and_store("link_01", generation=0, timestamp_ns=timestamp_ns)

    assert result.success is False
    assert len(plugin._applier.calls) == 1
    assert len(client.ensure_calls) == 0
    assert store.get_link("link_01").last_apply_success is False