from __future__ import annotations

from edge_status_reporter.state import EdgeApplicationState


def test_state_reports_real_fastapi_queue_semantics_and_activity() -> None:
    values = iter((10, 20))
    state = EdgeApplicationState(
        edge_node_id="edge_01",
        model_version="bearing-v1",
        clock_ns=lambda: next(values),
    )

    initial = state.snapshot()
    state.touch_task_activity()
    state.touch_task_activity()
    current = state.snapshot()

    assert initial.queue_length == 0
    assert initial.last_task_activity_ns == 0
    assert current.queue_length == 0
    assert current.last_task_activity_ns == 20
    assert current.models[0].model_version == "bearing-v1"
    assert current.models[0].load_status == "LOADED"
