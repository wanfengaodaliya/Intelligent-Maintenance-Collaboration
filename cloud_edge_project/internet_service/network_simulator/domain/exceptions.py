"""Domain-specific exceptions exposed by the controller and plugins."""

from __future__ import annotations


class NetworkSimulatorError(Exception):
    """Base exception for expected network simulator failures."""


class ConfigurationError(NetworkSimulatorError):
    """Raised when YAML or environment configuration is invalid."""


class ToxiproxyUnavailableError(NetworkSimulatorError):
    """Raised when the Toxiproxy management API cannot be reached."""


class ProxyConflictError(NetworkSimulatorError):
    """Raised when an existing proxy differs from the configured endpoint."""


class ToxicOperationError(NetworkSimulatorError):
    """Raised when a toxic cannot be applied or removed."""


class ReportDeliveryError(NetworkSimulatorError):
    """Raised when a scheduler report cannot be delivered."""


class InvalidScoreConfigurationError(ConfigurationError):
    """Raised when reliability score settings are inconsistent."""


class PluginLifecycleError(NetworkSimulatorError):
    """Raised when a required plugin cannot initialize or start."""
