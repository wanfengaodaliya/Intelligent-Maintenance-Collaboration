from edge_runtime.service import EdgeRuntimeService


class _Config:
    control = type("Control", (), {"host": "127.0.0.1", "port": 1})()

    def validate(self) -> list[str]:
        return []


class _Lifecycle:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


class _Pipeline(_Lifecycle):
    @property
    def started(self) -> bool:
        return self.__dict__["started"]

    @started.setter
    def started(self, value: bool) -> None:
        self.__dict__["started"] = value


class _Server:
    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        return None


def test_runtime_can_disable_legacy_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr("edge_runtime.service.make_control_server", lambda *args: _Server())
    cache = _Lifecycle()
    pipeline = _Pipeline()
    mqtt = _Lifecycle()
    service = EdgeRuntimeService(
        config=_Config(),
        cache=cache,
        pipeline=pipeline,
        mqtt_ingress=mqtt,
        control_application=object(),
        heartbeat=None,
        maintenance=None,
    )

    service.start()
    service.stop()

    assert service.started is False
    assert cache.started is False
    assert pipeline.started is False
    assert mqtt.started is False


def test_runtime_starts_and_stops_model_update_poller(monkeypatch) -> None:
    monkeypatch.setattr("edge_runtime.service.make_control_server", lambda *args: _Server())
    poller = _Lifecycle()
    service = EdgeRuntimeService(
        config=_Config(),
        cache=_Lifecycle(),
        pipeline=_Pipeline(),
        mqtt_ingress=_Lifecycle(),
        control_application=object(),
        heartbeat=None,
        maintenance=None,
        model_update_poller=poller,
    )

    service.start()
    assert poller.started is True

    service.stop()
    assert poller.started is False
