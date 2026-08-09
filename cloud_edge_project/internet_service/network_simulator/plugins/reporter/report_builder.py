"""Build Scheduler wire reports from immutable Runtime Store snapshots."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from controller.config_loader import ApplicationConfig
from domain.models import LinkSnapshot, RuntimeSnapshot

from .schemas import NetworkLinkReport, NetworkReport


class ReportBuilder:
    def __init__(self, config: ApplicationConfig) -> None:
        self._experiment_id = config.experiment.experiment_id
        self._reporter_id = config.reporter.reporter_id
        self._timezone = ZoneInfo(config.experiment.timezone)
        self._update_interval_seconds = config.controller.update_interval_seconds
        self._report_only_enabled_links = config.reporter.report_only_enabled_links

    def build(
        self,
        snapshot: RuntimeSnapshot,
        report_sequence: int,
        now_ns: int,
    ) -> NetworkReport:
        if report_sequence < 1:
            raise ValueError("report_sequence must be at least one")
        if now_ns < 0:
            raise ValueError("now_ns cannot be negative")
        links = tuple(
            self._build_link(link, now_ns)
            for link in snapshot.links
            if not self._report_only_enabled_links or link.report_enabled
        )
        generated_at = datetime.fromtimestamp(
            now_ns / 1_000_000_000,
            tz=self._timezone,
        )
        return NetworkReport(
            schema_version="1.0",
            experiment_id=self._experiment_id,
            reporter_id=self._reporter_id,
            report_sequence=report_sequence,
            generated_at=generated_at,
            generated_at_ns=now_ns,
            update_interval_seconds=self._update_interval_seconds,
            links=links,
        )

    @staticmethod
    def _build_link(link: LinkSnapshot, now_ns: int) -> NetworkLinkReport:
        applied = link.applied_parameters
        if applied is None:
            state = link.current_state
            state_since_ns = link.state_since_ns
            latency_ms = None
            jitter_ms = None
            bandwidth_kbps = None
            packet_loss_percent = None
        else:
            state = applied.state
            if link.applied_state_since_ns is None:
                raise ValueError(
                    f"link {link.link_id} has applied parameters without applied state time"
                )
            state_since_ns = link.applied_state_since_ns
            latency_ms = applied.latency_ms
            jitter_ms = applied.jitter_ms
            bandwidth_kbps = applied.bandwidth_kbps
            packet_loss_percent = applied.packet_loss_percent
        if state_since_ns > now_ns:
            raise ValueError(f"link {link.link_id} state time is in the future")
        return NetworkLinkReport(
            link_id=link.link_id,
            sender_id=link.sender_id,
            edge_id=link.edge_id,
            protocol=link.protocol,
            current_state=state,
            state_since_ns=state_since_ns,
            state_duration_ms=(now_ns - state_since_ns) // 1_000_000,
            latency_ms=latency_ms,
            jitter_ms=jitter_ms,
            bandwidth_kbps=bandwidth_kbps,
            packet_loss_percent=packet_loss_percent,
            link_reliability_score=link.link_reliability_score,
            available=link.available,
            last_apply_success=link.last_apply_success,
            last_apply_timestamp_ns=link.last_apply_timestamp_ns,
            consecutive_apply_failures=link.consecutive_apply_failures,
            error=link.last_error,
        )
