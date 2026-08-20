# -*- coding: utf-8 -*-
"""NET-4：RuntimeStore 状态切换的“立即悲观化”验证。

覆盖：GOOD→DISCONNECTED 不公开 available=true；GOOD→GOOD 同态不清零；
DISCONNECTED→GOOD 不提前恢复；随后正常 score 更新可恢复。
"""
from __future__ import annotations

from controller.runtime_store import RuntimeStore
from domain.enums import DisconnectMode, LinkProtocol, LinkType, NetworkState
from domain.models import SCORE_COMPONENT_NAMES, LinkRuntime, NetworkParameters


def _good_params(state: NetworkState) -> NetworkParameters:
    return NetworkParameters(
        state=state,
        latency_ms=20,
        jitter_ms=5,
        bandwidth_kbps=12_000,
        packet_loss_percent=0.0,
        disconnect_mode=DisconnectMode.NONE,
    )


def _disconnected_params() -> NetworkParameters:
    return NetworkParameters(
        state=NetworkState.DISCONNECTED,
        latency_ms=None,
        jitter_ms=None,
        bandwidth_kbps=None,
        packet_loss_percent=100.0,
        disconnect_mode=DisconnectMode.TIMEOUT,
    )


def _good_components() -> dict[str, float]:
    return {name: 90.0 for name in SCORE_COMPONENT_NAMES}


def _make_store(current_state: NetworkState = NetworkState.GOOD) -> RuntimeStore:
    runtime = LinkRuntime(
        link_id="link_01",
        link_type=LinkType.SENDER_TO_EDGE,
        sender_id="sender_01",
        edge_id="edge_01",
        protocol=LinkProtocol.MQTT,
        proxy_name="link_01_proxy",
        listen="0.0.0.0:22222",
        advertised_host="edge_01",
        advertised_port=22222,
        upstream="edge_01:1883",
        current_state=current_state,
        previous_state=current_state,
        state_since_ns=0,
        seed=1,
        desired_parameters=_good_params(current_state),
    )
    return RuntimeStore([runtime])


def _bless_good(store: RuntimeStore, generation: int) -> None:
    """让 link_01 处于已应用、可用、高分数的 GOOD 基线。"""
    good = _good_params(NetworkState.GOOD)
    store.update_generated_state("link_01", NetworkState.GOOD, good, 1_000, generation)
    store.update_apply_result(
        "link_01",
        success=True,
        applied_parameters=good,
        timestamp_ns=1_100,
        generation=generation,
    )
    store.update_score(
        "link_01",
        score=90.0,
        components=_good_components(),
        available=True,
        generation=generation,
    )


def test_t1_good_to_disconnected_is_pessimistic_at_snapshot() -> None:
    """T1：GOOD→DISCONNECTED 后立即读 snapshot（不调用 update_score）。
    必须是 state=DISCONNECTED + available=false + reliability=0 + 组件清零。"""
    store = _make_store()
    _bless_good(store, 1)

    disconnect = _disconnected_params()
    store.update_generated_state(
        "link_01",
        NetworkState.DISCONNECTED,
        disconnect,
        generated_at_ns=2_000,
        generation=2,
    )

    snap = store.get_link("link_01")
    assert snap.current_state is NetworkState.DISCONNECTED
    assert snap.available is False
    assert snap.link_reliability_score == 0.0
    assert all(value == 0.0 for value in snap.score_components.values())


def test_t2_homogeneous_regeneration_keeps_score_and_available() -> None:
    """T2：GOOD→GOOD 只是 generation 增加，score/available 必须保留不清零。"""
    store = _make_store()
    _bless_good(store, 1)

    good = _good_params(NetworkState.GOOD)
    store.update_generated_state(
        "link_01",
        NetworkState.GOOD,
        good,
        generated_at_ns=2_000,
        generation=2,
    )

    snap = store.get_link("link_01")
    assert snap.current_state is NetworkState.GOOD
    assert snap.available is True
    assert snap.link_reliability_score == 90.0
    assert snap.score_components == _good_components()


def test_t3_disconnected_to_good_does_not_prematurely_recover() -> None:
    """T3：DISCONNECTED→GOOD 后立即读，state=GOOD 但 available 不提前恢复。"""
    store = _make_store()
    _bless_good(store, 1)

    disconnect = _disconnected_params()
    store.update_generated_state(
        "link_01",
        NetworkState.DISCONNECTED,
        disconnect,
        generated_at_ns=2_000,
        generation=2,
    )

    good = _good_params(NetworkState.GOOD)
    store.update_generated_state(
        "link_01",
        NetworkState.GOOD,
        good,
        generated_at_ns=3_000,
        generation=3,
    )

    snap = store.get_link("link_01")
    assert snap.current_state is NetworkState.GOOD
    # 安全悲观方向：不提前宣称可用，等待 apply+score 刷新后再恢复。
    assert snap.available is False
    assert snap.link_reliability_score == 0.0


def test_t4_normal_score_update_recovers_after_generation_validated() -> None:
    """T4：DISCONNECTED→GOOD 后继续 apply + update_score，可用性正常恢复。"""
    store = _make_store()
    _bless_good(store, 1)

    disconnect = _disconnected_params()
    store.update_generated_state(
        "link_01",
        NetworkState.DISCONNECTED,
        disconnect,
        generated_at_ns=2_000,
        generation=2,
    )

    good = _good_params(NetworkState.GOOD)
    store.update_generated_state(
        "link_01",
        NetworkState.GOOD,
        good,
        generated_at_ns=3_000,
        generation=3,
    )
    store.update_apply_result(
        "link_01",
        success=True,
        applied_parameters=good,
        timestamp_ns=3_100,
        generation=3,
    )
    store.update_score(
        "link_01",
        score=88.0,
        components=_good_components(),
        available=True,
        generation=3,
    )

    snap = store.get_link("link_01")
    assert snap.current_state is NetworkState.GOOD
    assert snap.available is True
    assert snap.link_reliability_score == 88.0