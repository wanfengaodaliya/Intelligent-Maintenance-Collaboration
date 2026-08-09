"""Stable V3 domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class NetworkState(StrEnum):
    GOOD = "GOOD"
    MEDIUM = "MEDIUM"
    BAD = "BAD"
    DISCONNECTED = "DISCONNECTED"


class LinkProtocol(StrEnum):
    MQTT = "mqtt"
    HTTP = "http"


class LinkType(StrEnum):
    SENDER_TO_EDGE = "sender_to_edge"
    SENDER_TO_SCHEDULER = "sender_to_scheduler"
    SCHEDULER_TO_SENDER = "scheduler_to_sender"
    EDGE_TO_SCHEDULER = "edge_to_scheduler"
    SCHEDULER_TO_EDGE = "scheduler_to_edge"
    EDGE_TO_CLOUD = "edge_to_cloud"
    SCHEDULER_TO_CLOUD = "scheduler_to_cloud"
    NETWORK_TO_SCHEDULER = "network_to_scheduler"


class ExperimentMode(StrEnum):
    MARKOV = "markov"
    FIXED = "fixed"


class DisconnectMode(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    RESET_PEER = "reset_peer"
