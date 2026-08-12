from __future__ import annotations

import asyncio
from types import SimpleNamespace

from edge_status_reporter.bootstrap import EdgeStatusIntegration, build_edge_status_integration


class FakeApp:
    def __init__(self) -> None:
        self.middleware: list[tuple[object, dict]] = []

    def add_middleware(self, middleware, **kwargs) -> None:
        self.middleware.append((middleware, kwargs))


class FakeReporter:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakePsutil:
    def cpu_count(self, logical=True) -> int:
        return 4

    def cpu_percent(self, interval=None) -> float:
        return 0.0

    def virtual_memory(self):
        return SimpleNamespace(available=1024 * 1024 * 1024)


def test_disabled_integration_installs_nothing() -> None:
    integration = build_edge_status_integration(
        edge_node_id="edge_01",
        default_model_version="model",
        environ={"EDGE_STATUS_REPORTER_ENABLED": "false"},
    )
    app = FakeApp()

    integration.install(app)

    assert integration.enabled is False
    assert app.middleware == []


def test_enabled_integration_installs_activity_middleware_once() -> None:
    integration = build_edge_status_integration(
        edge_node_id="edge_01",
        default_model_version="model",
        environ={},
        psutil_module=FakePsutil(),
        http_post=lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )
    app = FakeApp()

    integration.install(app)
    integration.install(app)

    assert integration.enabled is True
    assert len(app.middleware) == 1
    assert app.middleware[0][1]["state"] is integration.state


def test_lifespan_starts_and_stops_reporter() -> None:
    reporter = FakeReporter()
    integration = EdgeStatusIntegration(state=None, reporter=reporter)

    async def run() -> None:
        async with integration.lifespan(None):
            assert reporter.started == 1
            assert reporter.stopped == 0

    asyncio.run(run())
    assert reporter.stopped == 1


def test_invalid_disabled_target_values_do_not_disable_integration() -> None:
    integration = build_edge_status_integration(
        edge_node_id="edge_01",
        default_model_version="model",
        environ={
            "EDGE_STATUS_SCHEDULER_ENABLED": "false",
            "EDGE_STATUS_SCHEDULER_TIMEOUT_SECONDS": "invalid",
        },
        psutil_module=FakePsutil(),
        http_post=lambda *args, **kwargs: SimpleNamespace(status_code=200),
    )

    assert integration.enabled is True
    assert [target.name for target in integration.reporter.targets] == ["cloud"]
