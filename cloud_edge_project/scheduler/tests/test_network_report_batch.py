"""AUD-02: batch network report endpoint keeps every link and reports honest results."""

from scheduler.api import (
    RegistryError,
    update_network_report,
    update_network_report_batch,
)
from scheduler.node_registry import EdgeNodeConfig, NodeRegistry


def _registry(monkeypatch, *edge_ids):
    registry = NodeRegistry(
        {
            edge_id: EdgeNodeConfig(
                edge_id, f"http://127.0.0.1:8001", f"edge/{edge_id}/input"
            )
            for edge_id in edge_ids
        }
    )
    monkeypatch.setattr("scheduler.api.node_registry", registry)
    return registry


def _mqtt_link(link_id, sender_id, edge_id, **overrides):
    link = {
        "link_id": link_id,
        "sender_id": sender_id,
        "edge_id": edge_id,
        "protocol": "mqtt",
        "latency_ms": 20,
        "jitter_ms": 5,
        "bandwidth_kbps": 12_000,
        "packet_loss_percent": 1.0,
        "available": True,
        "last_apply_success": True,
    }
    link.update(overrides)
    return link


def _http_link(link_id):
    return {
        "link_id": link_id,
        "sender_id": None,
        "edge_id": None,
        "protocol": "http",
        "available": True,
        "last_apply_success": True,
    }


def test_batch_with_three_valid_links_updates_all_snapshots(monkeypatch):
    registry = _registry(monkeypatch, "edge_01")
    payload = {
        "report_sequence": 1,
        "generated_at_ns": 1_000,
        "links": [
            _mqtt_link("sender_01__to__edge_01__mqtt", "sender_01", "edge_01"),
            _mqtt_link("sender_02__to__edge_01__mqtt", "sender_02", "edge_01"),
            _mqtt_link("sender_03__to__edge_01__mqtt", "sender_03", "edge_01"),
        ],
    }

    acknowledgement = update_network_report_batch(payload)

    assert acknowledgement["accepted"] is True
    assert acknowledgement["received_count"] == 3
    assert acknowledgement["accepted_count"] == 3
    assert acknowledgement["rejected_count"] == 0
    assert acknowledgement["skipped_count"] == 0
    assert acknowledgement["results"] == []
    for sender_id in ("sender_01", "sender_02", "sender_03"):
        snapshot = registry.link_snapshot(sender_id, "edge_01", now_ns=1)
        assert snapshot is not None, f"{sender_id} -> edge_01 missing"
        assert snapshot.rtt_ms_avg == 20.0


def test_batch_with_full_eighteen_link_report_applies_all_mqtt_links(monkeypatch):
    """The full 18-link simulator report: 6 MQTT links land, 12 HTTP links are skipped by design."""
    registry = _registry(monkeypatch, "edge_01", "edge_02")
    mqtt_links = [
        _mqtt_link(
            f"sender_0{sender}__to__edge_0{edge}__mqtt",
            f"sender_0{sender}",
            f"edge_0{edge}",
        )
        for sender in (1, 2, 3)
        for edge in (1, 2)
    ]
    http_links = [
        _http_link(link_id)
        for link_id in (
            "sender_01__to__scheduler__http",
            "sender_02__to__scheduler__http",
            "sender_03__to__scheduler__http",
            "edge_01__to__scheduler__http",
            "scheduler__to__edge_01__http",
            "edge_01__to__cloud__http",
            "cloud__to__edge_01__http",
            "cloud__to__scheduler__http",
            "edge_02__to__scheduler__http",
            "scheduler__to__edge_02__http",
            "edge_02__to__cloud__http",
            "cloud__to__edge_02__http",
        )
    ]
    assert len(mqtt_links) == 6
    assert len(http_links) == 12

    acknowledgement = update_network_report_batch(
        {
            "report_sequence": 2,
            "generated_at_ns": 2_000,
            "links": mqtt_links + http_links,
        }
    )

    assert acknowledgement["accepted"] is True
    assert acknowledgement["received_count"] == 18
    assert acknowledgement["accepted_count"] == 6
    assert acknowledgement["skipped_count"] == 12
    assert acknowledgement["rejected_count"] == 0
    # Every sender→edge MQTT link, including edge_02, must reach the registry.
    for sender_id in ("sender_01", "sender_02", "sender_03"):
        for edge_id in ("edge_01", "edge_02"):
            assert (
                registry.link_snapshot(sender_id, edge_id, now_ns=1) is not None
            ), f"{sender_id} -> {edge_id} missing"


def test_batch_with_one_unregistered_edge_reports_partial_failure(monkeypatch):
    registry = _registry(monkeypatch, "edge_01", "edge_02")
    links = [
        _mqtt_link(
            f"sender_0{sender}__to__edge_01__mqtt", f"sender_0{sender}", "edge_01"
        )
        for sender in (1, 2, 3)
    ]
    # 17 valid links targeting edge_01 plus one link for an unregistered edge_02.
    links.extend(
        _mqtt_link(
            f"sender_0{sender}__to__edge_0{edge}__mqtt",
            f"sender_0{sender}",
            f"edge_0{edge}",
        )
        for sender in (1, 2, 3)
        for edge in (2,)
    )
    # Pad with distinct senders so the batch has exactly 18 links, 17 valid.
    for index in range(11):
        links.append(
            _mqtt_link(
                f"bulk_{index}__to__edge_01__mqtt", f"bulk_{index}", "edge_01"
            )
        )
    links.append(_mqtt_link("sender_01__to__edge_99__mqtt", "sender_01", "edge_99"))
    assert len(links) == 18

    acknowledgement = update_network_report_batch(
        {"report_sequence": 3, "generated_at_ns": 3_000, "links": links}
    )

    assert acknowledgement["accepted"] is False
    assert acknowledgement["received_count"] == 18
    assert acknowledgement["accepted_count"] == 17
    assert acknowledgement["rejected_count"] == 1
    assert acknowledgement["results"] == [
        {
            "link_id": "sender_01__to__edge_99__mqtt",
            "accepted": False,
            "reason": "unregistered_edge_node",
        }
    ]
    # The 17 valid links are still written despite the single rejection.
    assert (
        registry.link_snapshot("bulk_10", "edge_01", now_ns=1) is not None
    )
    assert registry.link_snapshot("sender_01", "edge_02", now_ns=1) is not None
    assert registry.link_snapshot("sender_01", "edge_99", now_ns=1) is None


def test_batch_with_only_invalid_links_is_not_accepted(monkeypatch):
    registry = _registry(monkeypatch, "edge_01")

    acknowledgement = update_network_report_batch(
        {
            "report_sequence": 4,
            "generated_at_ns": 4_000,
            "links": [
                _mqtt_link("sender_01__to__edge_99__mqtt", "sender_01", "edge_99"),
                _mqtt_link("sender_02__to__edge_98__mqtt", "sender_02", "edge_98"),
                # Missing sender_id/edge_id -> invalid link entry.
                {"link_id": "broken__to__edge_01__mqtt", "protocol": "mqtt"},
                # Non-dict entry -> invalid link entry.
                "not-a-mapping",
            ],
        }
    )

    assert acknowledgement["accepted"] is False
    assert acknowledgement["received_count"] == 4
    assert acknowledgement["accepted_count"] == 0
    assert acknowledgement["rejected_count"] == 4
    reasons = {item["reason"] for item in acknowledgement["results"]}
    assert reasons == {"unregistered_edge_node", "invalid_link"}
    assert registry.link_snapshot("sender_01", "edge_01", now_ns=1) is None


def test_batch_rejects_malformed_request_body(monkeypatch):
    _registry(monkeypatch, "edge_01")

    for payload in ({}, {"links": []}, {"report_sequence": 1}, None):
        try:
            update_network_report_batch(payload)
        except RegistryError as error:
            assert error.code == "INVALID_LINK_SNAPSHOT"
        else:
            raise AssertionError(f"expected RegistryError for {payload!r}")


def test_batch_duplicate_delivery_is_idempotent(monkeypatch):
    """A retried identical report must not be reported as rejected (stale duplicates count as confirmed)."""
    _registry(monkeypatch, "edge_01")
    payload = {
        "report_sequence": 5,
        "generated_at_ns": 5_000,
        "links": [
            _mqtt_link("sender_01__to__edge_01__mqtt", "sender_01", "edge_01"),
            _mqtt_link("sender_02__to__edge_01__mqtt", "sender_02", "edge_01"),
        ],
    }

    first = update_network_report_batch(payload)
    second = update_network_report_batch(payload)

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["accepted_count"] == 2
    assert second["rejected_count"] == 0


def test_legacy_single_link_endpoint_still_filters_selected_link(monkeypatch):
    registry = _registry(monkeypatch, "edge_01")
    payload = {
        "report_sequence": 6,
        "generated_at_ns": 6_000,
        "links": [
            _mqtt_link("sender_01__to__edge_01__mqtt", "sender_01", "edge_01"),
            _mqtt_link("sender_02__to__edge_01__mqtt", "sender_02", "edge_01"),
        ],
    }

    acknowledgement = update_network_report(
        payload, selected_link_ids={"sender_01__to__edge_01__mqtt"}
    )

    assert acknowledgement == {"accepted": True, "report_sequence": 6}
    assert registry.link_snapshot("sender_01", "edge_01", now_ns=1) is not None
    assert registry.link_snapshot("sender_02", "edge_01", now_ns=1) is None
