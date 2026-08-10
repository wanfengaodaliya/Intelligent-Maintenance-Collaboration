"""Load confirmed package-routing thresholds from the shared local configuration."""

from __future__ import annotations

import os

from common.config import load_config

try:
    from .packet_router import PacketRoutingConfig
except ImportError:
    from packet_router import PacketRoutingConfig


def load_packet_routing_config() -> PacketRoutingConfig:
    config = load_config()
    packet = config.get("packet_routing", {})
    cloud = config.get("cloud_node", {})
    network = config.get("cloud_network", {})
    required_model = os.getenv("SCHEDULER_REQUIRED_CLOUD_MODEL", "").strip() or None
    return PacketRoutingConfig(
        confidence_threshold=float(packet.get("confidence_threshold", 0.80)),
        max_cloud_queue_length=int(cloud.get("max_queue_length", 5)),
        min_uplink_mbps=float(network.get("min_uplink_mbps", 2.0)),
        max_rtt_p95_ms=float(network.get("max_rtt_p95_ms", 100.0)),
        max_loss_rate=float(network.get("max_loss_rate", 0.10)),
        required_cloud_model=required_model,
        default_cloud_node_id=os.getenv("SCHEDULER_CLOUD_NODE_ID", "cloud_1").strip(),
    )
