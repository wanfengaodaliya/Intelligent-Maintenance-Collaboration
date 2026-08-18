# Edge Packet Cloud Review Closed Loop Implementation Plan
<!-- 本文档说明边缘单包云端复核闭环的分步实现与验证计划。 -->

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect edge packet completion, scheduler packet routing, durable cloud-review dispatch, edge-owned raw upload, and scheduler result reporting into one recoverable closed loop.

**Architecture:** A scheduler runtime container owns assignment, packet-routing, cloud-state, persistence, and worker lifecycles; FastAPI and the direct HTTP server share that container. The edge coordinator converts each `PacketExecutionCompleted` into a durable packet record plus a formal scheduler request, while all cloud-required routes execute through the same SQLite-backed dispatcher so there is only one upload path.

**Tech Stack:** Python 3, FastAPI lifespan, SQLite, `requests`, threaded background workers, pytest, temporary filesystem fixtures.

## Global Constraints

- Preserve the `POST /scheduler/decide` V0.1 request, response, assignment, reservation, edge-ACK, and `target_topic` contract.
- Restrict `cloud_1` to `cloud_01` normalization to the packet-review path; do not modify `rule_scheduler.py`, `infer_cloud_v01`, or the V0.1 `cloud_service/mock_backend.py` identity.
- The scheduler stores control metadata and references only; raw sampled arrays remain edge-owned and upload directly from edge to cloud.
- Do not change device-level three-bearing arbitration, aggregation timing, or deferred-device dispatch.
- Use the existing thresholds: confidence `0.80`, cloud queue `5`, uplink `2.0 Mbps`, RTT P95 `100 ms`, loss `0.10`, status TTL `5 seconds`, retention `24 hours`.
- Use the existing retry schedule: `5`, `10`, `20`, `40`, then `60` seconds for subsequent attempts.
- Every production change follows RED -> GREEN -> REFACTOR, and each RED run must fail for the behavior named by the test.
- Use temporary SQLite databases and temporary edge stores in tests; do not read or write `cloud_edge_project/data/scheduler.db` or existing runtime artifacts.

---

## File Structure

- Create `scheduler/runtime.py`: construct and own scheduler registries, repositories, services, dispatchers, and their idempotent lifecycle.
- Modify `scheduler/api.py`: expose existing assignment endpoints plus packet-review endpoints through an app factory backed by `SchedulerRuntime`.
- Modify `scheduler/node_registry.py`: expose a public edge control URL lookup and keep sender-to-edge snapshots separate from edge-to-cloud snapshots.
- Modify `scheduler/deferred_cloud_repository.py`: provide the authoritative current task state needed after synchronous edge callbacks.
- Modify `scheduler/deferred_dispatcher.py`: reconcile HTTP completion against repository state instead of blindly marking dispatched.
- Modify `scheduler/routing_config.py`: use packet-path `cloud_01` consistently.
- Modify `edge_service/src/packet_routing_bridge.py`: consume `PacketExecutionCompleted`, persist the raw packet first, and emit the exact packet-route contract.
- Create `edge_service/src/edge_runtime/packet_route_reporter.py`: perform a bounded retry of the scheduler packet-route HTTP request without creating a second durable queue or cloud-upload path.
- Modify `edge_service/src/edge_runtime/coordinator.py`: call the packet bridge for every terminal packet without removing the existing aggregation path.
- Modify `edge_service/src/edge_runtime/factory.py`: construct and inject the packet bridge with the existing scheduler HTTP client and cloud-review store.
- Modify `edge_service/src/edge_runtime/config.py`: use `/scheduler/edge-nodes/status` and carry packet-route retry settings.
- Modify `edge_service/app.py`: build one edge cloud-review store and manage cleanup through lifespan.
- Modify `cloud_service/app.py`: run `CloudNodeStatusReporter` through lifespan without changing V0.1 inference.
- Modify `configs/local.yaml`: restore packet routing, cloud state, network threshold, and deferred-review settings.
- Create `scheduler/tests/test_packet_runtime_api.py`: scheduler API, link routing, and lifecycle behavior.
- Create `scheduler/tests/test_deferred_dispatcher_race.py`: synchronous callback state reconciliation.
- Create `edge_service/verification/test_packet_routing_bridge.py`: durable-first edge packet adaptation and coordinator integration.
- Create `verification/test_packet_cloud_review_closed_loop.py`: temporary-store, temporary-database in-process closed loop.
- Create or modify local `conftest.py` only where import-path isolation is required by the new test directory.

---

### Task 1: Scheduler runtime container and packet APIs

**Files:**
- Create: `cloud_edge_project/scheduler/runtime.py`
- Modify: `cloud_edge_project/scheduler/api.py`
- Modify: `cloud_edge_project/scheduler/node_registry.py`
- Modify: `cloud_edge_project/scheduler/routing_config.py`
- Modify: `cloud_edge_project/configs/local.yaml`
- Create: `cloud_edge_project/scheduler/tests/test_packet_runtime_api.py`
- Create: `cloud_edge_project/scheduler/tests/conftest.py`

**Interfaces:**
- Consumes: `AssignmentScheduler`, `CloudNodeRegistry`, `DeferredCloudRepository`, `DeferredCloudDispatcher`, `NodeRegistry`, `PacketRouter`, `PacketRoutingService`, `TaskRepository`.
- Produces: `SchedulerRuntime.start() -> None`, `SchedulerRuntime.stop() -> None`, `SchedulerRuntime.update_link_snapshot(payload) -> dict`, `create_app(runtime: SchedulerRuntime | None = None) -> FastAPI`, and module-level `app`.

- [ ] **Step 1: Write scheduler API and lifecycle failure tests**

Create tests that inspect real FastAPI routes and exercise the application lifespan with a recording runtime:

```python
from fastapi.testclient import TestClient

from scheduler.api import create_app


class RecordingRuntime:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def health(self):
        return {"status": "ok"}


def test_packet_routes_are_registered():
    app = create_app(RecordingRuntime())
    paths = {route.path for route in app.routes}
    assert "/scheduler/packet-route" in paths
    assert "/scheduler/cloud-nodes/status" in paths
    assert "/scheduler/cloud-upload-results" in paths


def test_fastapi_lifespan_starts_and_stops_scheduler_runtime():
    runtime = RecordingRuntime()
    with TestClient(create_app(runtime)):
        assert runtime.events == ["start"]
    assert runtime.events == ["start", "stop"]
```

The test double must also implement the endpoint methods used by route registration so failures name missing production interfaces, not test setup errors.

- [ ] **Step 2: Write edge/cloud link split and packet cloud ID tests**

Use real registries and literal payloads:

```python
def test_edge_to_cloud_link_is_saved_in_cloud_registry(runtime, cloud_link_payload):
    result = runtime.update_link_snapshot(cloud_link_payload)
    saved = runtime.cloud_registry.link_snapshot("edge_01", "cloud_01")
    assert result["accepted"] is True
    assert saved is not None
    assert saved.link_id == "edge_01_to_cloud_01"


def test_packet_router_defaults_to_cloud_01(runtime):
    assert runtime.packet_router.config.default_cloud_node_id == "cloud_01"
```

Also retain a sender-to-edge snapshot test proving it still reaches `NodeRegistry`.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
& 'E:\python\python.exe' -m pytest scheduler/tests/test_packet_runtime_api.py -q
```

Expected: FAIL because `scheduler.api.create_app`, `scheduler.runtime.SchedulerRuntime`, packet endpoints, or packet-path `cloud_01` wiring do not exist.

- [ ] **Step 4: Implement `SchedulerRuntime`**

Create a focused container with dependency injection for tests:

```python
class SchedulerRuntime:
    def __init__(self, *, database_path=None, config=None, dispatcher_client=None):
        self.cloud_registry = CloudNodeRegistry(status_ttl_ns=...)
        self.node_registry = NodeRegistry()
        self.task_repository = TaskRepository(database_path)
        self.deferred_repository = DeferredCloudRepository(self.task_repository.database_path)
        self.assignment_scheduler = AssignmentScheduler(self.node_registry, self.task_repository)
        self.packet_router = PacketRouter(
            assignment_lookup=self.task_repository.get,
            cloud_registry=self.cloud_registry,
            config=load_packet_routing_config(),
        )
        self.packet_service = PacketRoutingService(
            self.packet_router,
            self.deferred_repository,
        )
        self.deferred_dispatcher = DeferredCloudDispatcher(
            self.deferred_repository,
            edge_url_lookup=self.node_registry.control_url,
            client=dispatcher_client,
            eligibility_check=self.packet_router.cloud_delivery_eligibility,
        )
        self._started = False

    def start(self):
        if self._started:
            return
        self.node_registry.start_monitor()
        self.deferred_dispatcher.start(self.dispatcher_interval_seconds)
        self._started = True

    def stop(self):
        if not self._started:
            return
        self.deferred_dispatcher.stop()
        self.node_registry.stop_monitor()
        self._started = False
```

Add `NodeRegistry.control_url(edge_node_id: str) -> str`, using its lock and raising `UNREGISTERED_EDGE_NODE` instead of accessing `_nodes` from `api.py`.

- [ ] **Step 5: Route link snapshots by contract shape**

Implement `SchedulerRuntime.update_link_snapshot()`:

```python
def update_link_snapshot(self, payload):
    cloud_fields = {"link_id", "source_id", "target_id"}
    if cloud_fields <= set(payload):
        return self.cloud_registry.update_link(payload)
    return self.node_registry.update_link(payload)
```

Do not add cloud-specific state back into `NodeRegistry`; the runtime is the orchestration boundary.

- [ ] **Step 6: Build the FastAPI app from the runtime**

Refactor `scheduler/api.py` so `create_app()` registers existing assignment endpoints and the three packet endpoints. Its lifespan calls `runtime.start()` and `runtime.stop()`. Module-level functions delegate to a default runtime for the standard-library handler. `run()` starts and stops the same runtime in `try/finally`.

- [ ] **Step 7: Restore packet-review configuration**

Restore the exact YAML keys and thresholds from the approved design. Change only `load_packet_routing_config()` default from `cloud_1` to `cloud_01`; do not edit `rule_scheduler.py`, `infer_cloud_v01`, or `cloud_service/mock_backend.py`.

- [ ] **Step 8: Run Task 1 tests and assignment regression**

Run:

```powershell
& 'E:\python\python.exe' -m pytest scheduler/tests/test_packet_runtime_api.py -q
& 'E:\python\python.exe' -m pytest -q -k 'assignment or scheduler_decide'
```

Expected: Task 1 tests PASS. Record any unrelated collection dependency failure separately instead of weakening assertions.

- [ ] **Step 9: Commit Task 1**

```powershell
git add -- cloud_edge_project/scheduler/runtime.py cloud_edge_project/scheduler/api.py cloud_edge_project/scheduler/node_registry.py cloud_edge_project/scheduler/routing_config.py cloud_edge_project/configs/local.yaml cloud_edge_project/scheduler/tests
git commit -m "feat: restore scheduler packet review runtime"
```

---

### Task 2: Synchronous upload-result race reconciliation

**Files:**
- Modify: `cloud_edge_project/scheduler/deferred_cloud_repository.py`
- Modify: `cloud_edge_project/scheduler/deferred_dispatcher.py`
- Create: `cloud_edge_project/scheduler/tests/test_deferred_dispatcher_race.py`

**Interfaces:**
- Consumes: `DeferredCloudRepository.get(decision_id) -> dict | None` and existing `save_upload_result()` state transitions.
- Produces: `DeferredCloudDispatcher.dispatch_once()` returning the repository's authoritative post-dispatch task without overwriting synchronous callbacks.

- [ ] **Step 1: Write four real-repository race tests**

Use a temporary SQLite database and a client whose `dispatch()` calls `repository.save_upload_result()` before returning. Cover literal statuses:

```python
@pytest.mark.parametrize(
    ("upload_status", "review_id", "reason_code", "expected_state"),
    [
        ("SUCCESS", "review_01", None, "SUCCEEDED"),
        ("RETRYABLE_FAILED", None, "CLOUD_TIMEOUT", "PENDING"),
        ("PERMANENT_FAILED", None, "INVALID_CLOUD_REQUEST", "PERMANENT_FAILED"),
    ],
)
def test_synchronous_result_is_authoritative(...):
    result = dispatcher.dispatch_once(now_ns=NOW_NS)
    assert result["state"] == expected_state
    assert repository.get(DECISION_ID)["state"] == expected_state


def test_no_synchronous_result_moves_dispatching_to_waiting_result(...):
    result = dispatcher.dispatch_once(now_ns=NOW_NS)
    assert result["state"] == "WAITING_RESULT"
```

- [ ] **Step 2: Run the race tests and verify RED**

Run:

```powershell
& 'E:\python\python.exe' -m pytest scheduler/tests/test_deferred_dispatcher_race.py -q
```

Expected: synchronous retry case FAILS with `INVALID_DEFERRED_STATE`; synchronous success/permanent cases expose the same unconditional transition risk.

- [ ] **Step 3: Implement authoritative state reconciliation**

After a successful edge HTTP response, read the task:

```python
current = self.repository.get(task["decision_id"])
if current is None:
    raise DeferredCloudError(
        "DEFERRED_TASK_NOT_FOUND",
        "decision disappeared after edge dispatch",
        404,
    )
if current["state"] != "DISPATCHING":
    return current
return self.repository.mark_dispatched(task["decision_id"], now_ns=now)
```

For HTTP exceptions, re-read state before `schedule_retry()` as well. If a synchronous callback already produced a different non-dispatching state, return it. This prevents a late HTTP exception wrapper from overwriting a callback that reached the scheduler.

- [ ] **Step 4: Run Task 2 tests**

Run:

```powershell
& 'E:\python\python.exe' -m pytest scheduler/tests/test_deferred_dispatcher_race.py -q
& 'E:\python\python.exe' -m pytest scheduler/tests/test_packet_runtime_api.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- cloud_edge_project/scheduler/deferred_cloud_repository.py cloud_edge_project/scheduler/deferred_dispatcher.py cloud_edge_project/scheduler/tests/test_deferred_dispatcher_race.py
git commit -m "fix: reconcile synchronous cloud review callbacks"
```

---

### Task 3: Durable-first edge packet routing bridge

**Files:**
- Modify: `cloud_edge_project/edge_service/src/packet_routing_bridge.py`
- Create: `cloud_edge_project/edge_service/src/edge_runtime/packet_route_reporter.py`
- Modify: `cloud_edge_project/edge_service/src/edge_runtime/coordinator.py`
- Modify: `cloud_edge_project/edge_service/src/edge_runtime/factory.py`
- Modify: `cloud_edge_project/edge_service/src/edge_runtime/config.py`
- Modify: `cloud_edge_project/edge_service/app.py`
- Create: `cloud_edge_project/edge_service/verification/test_packet_routing_bridge.py`

**Interfaces:**
- Consumes: `PacketExecutionCompleted`, `CloudReviewStore.save()`, scheduler HTTP `post(path, payload)`, and the existing aggregation workflow.
- Produces: `PacketRoutingBridge.route(raw_packet, completion) -> dict`, `PacketRouteReporter.report(payload) -> dict` with at most three attempts, coordinator routing error records that leave the raw packet durable, and no second direct cloud-upload branch.

- [ ] **Step 1: Write successful completion contract test**

Create a real temporary `CloudReviewStore`, a literal raw packet fixture, a `PacketExecutionCompleted(status="SUCCEEDED")`, and a recording scheduler boundary:

```python
result = bridge.route(raw_packet, completion)

stored = store.get("task_01", "bearing_01", "packet_01")
assert stored is not None
assert calls[0][0] == "/scheduler/packet-route"
assert calls[0][1]["output"] == {
    "edge_result": "warning",
    "confidence": 0.72,
    "task_complexity": 0.28,
    "edge_risk_level": "medium",
    "model_version": "edge_v1.0",
}
assert result == {"route": "EDGE_PROVISIONAL_AND_DEFER_CLOUD"}
```

The recording post function first asserts `store.get(...) is not None`; this proves durable-before-report ordering.

- [ ] **Step 2: Write failed completion and scheduler-unavailable tests**

For `status="FAILED"`, assert `error` is non-empty and `output` is absent. For scheduler failure:

```python
with pytest.raises(HttpRequestError):
    bridge.route(raw_packet, completion)
assert store.get("task_01", "bearing_01", "packet_01") is not None
```

Name the mutation caught: moving `store.save()` after scheduler POST must fail both tests.

Add reporter retry cases with a fake transport that raises `HttpRequestError(retryable=True)` twice and succeeds on the third call. Assert the returned scheduler response and exactly three calls. Add a non-retryable case that stops after one call. The production reporter uses delays `(0.05, 0.10)` seconds between the three total attempts and accepts an injected wait function so tests do not sleep.

- [ ] **Step 3: Write coordinator integration test**

Construct `EdgeRuntimeCoordinator` with a recording packet bridge and recording aggregation workflow. Invoke `on_packet_completed()` and assert:

```python
assert events == ["record_completion", "packet_route", "aggregate"]
```

When packet routing raises, assert aggregation still executes and the coordinator records a route error through an injected callback. Do not silently swallow without an observable error record.

- [ ] **Step 4: Run Task 3 tests and verify RED**

Run:

```powershell
& 'E:\python\python.exe' -m pytest edge_service/verification/test_packet_routing_bridge.py -q
```

Expected: FAIL because `PacketRoutingBridge` accepts the old `simple_result`, the coordinator has no bridge dependency, and the factory does not inject it.

- [ ] **Step 5: Refactor `PacketRoutingBridge` around completion events**

Implement the public method:

```python
def route(
    self,
    raw_packet: Mapping[str, Any],
    completion: PacketExecutionCompleted,
) -> dict[str, Any]:
    persisted_result = self._persisted_result(completion)
    self.store.save(raw_packet, persisted_result)
    return self.post(
        "/scheduler/packet-route",
        self._request(completion),
    )
```

Map `SUCCEEDED`, `FAILED`, and `TIMEOUT` exactly. Preserve the completion timestamps and compute complexity with `round(1.0 - confidence, 6)`.

- [ ] **Step 6: Inject bridge and observable error reporting**

Add optional coordinator dependencies:

```python
packet_router: Optional[PacketRoutingBridge] = None
on_packet_route_error: Callable[[dict[str, Any]], None] = lambda _: None
```

After `record_packet_completion()`, obtain the raw packet from the validation cache URI or packet ref, invoke the bridge, emit a structured error record on failure, then always continue the existing aggregation call.

The factory constructs the bridge using a `PacketRouteReporter` over the same scheduler HTTP client boundary and the same `CloudReviewStore` exposed to `/edge/cloud-review-tasks`. Pass the store into `build_edge_runtime()` instead of constructing two independent stores. Exhausted scheduler retries emit a structured route error and leave the raw packet in the store for audit or replay; they do not start cloud upload independently.

- [ ] **Step 7: Move edge cleanup to lifespan and align heartbeat**

Change `SchedulerConfig.status_path` to `/scheduler/edge-nodes/status`. Stop starting `CloudReviewCleanupWorker` at import time. The edge FastAPI lifespan starts runtime service and cleanup worker, then stops both in `finally`.

- [ ] **Step 8: Run Task 3 tests and existing edge regressions**

Run:

```powershell
& 'E:\python\python.exe' -m pytest edge_service/verification/test_packet_routing_bridge.py -q
& 'E:\python\python.exe' -m pytest edge_service/verification/test_bearing_aggregation.py edge_service/verification/test_edge_control_cleanup.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 9: Commit Task 3**

```powershell
git add -- cloud_edge_project/edge_service/app.py cloud_edge_project/edge_service/src/packet_routing_bridge.py cloud_edge_project/edge_service/src/edge_runtime/packet_route_reporter.py cloud_edge_project/edge_service/src/edge_runtime/config.py cloud_edge_project/edge_service/src/edge_runtime/factory.py cloud_edge_project/edge_service/src/edge_runtime/coordinator.py cloud_edge_project/edge_service/verification/test_packet_routing_bridge.py
git commit -m "feat: connect edge packet results to scheduler routing"
```

---

### Task 4: Cloud status reporting lifecycle

**Files:**
- Modify: `cloud_edge_project/cloud_service/app.py`
- Create: `cloud_edge_project/cloud_service/tests/test_status_reporter_lifecycle.py`

**Interfaces:**
- Consumes: existing `CloudNodeStatusReporter.run_forever()`, `load_cloud_settings()`, and scheduler endpoint `/scheduler/cloud-nodes/status`.
- Produces: one lifespan-managed async reporter task using packet-path node ID `cloud_01` without altering V0.1 inference payloads.

- [ ] **Step 1: Write cloud lifespan failure test**

Inject a recording reporter into an application factory or reporter factory seam:

```python
def test_cloud_lifespan_runs_and_cancels_status_reporter():
    reporter = RecordingReporter()
    with TestClient(create_app(status_reporter=reporter)):
        assert reporter.started.wait(timeout=1)
    assert reporter.cancelled is True
```

Add a payload-level test asserting the configured packet-review node ID is exactly `cloud_01`.

- [ ] **Step 2: Run the new cloud test and verify RED**

Run:

```powershell
& 'E:\python\python.exe' -m pytest cloud_service/tests/test_status_reporter_lifecycle.py -q
```

Expected: FAIL because `CloudNodeStatusReporter` is not created or scheduled by `cloud_service/app.py`.

- [ ] **Step 3: Add lifespan-managed reporter task**

Extend the existing cloud lifespan:

```python
status_task = asyncio.create_task(status_reporter.run_forever())
try:
    yield
finally:
    status_task.cancel()
    with suppress(asyncio.CancelledError):
        await status_task
```

Use `SCHEDULER_SERVICE_BASE_URL` default `http://127.0.0.1:8003` and packet-review `CLOUD_REVIEW_NODE_ID` default `cloud_01`. Do not edit `CLOUD_NODE_ID = "cloud_1"` in `mock_backend.py`.

- [ ] **Step 4: Run cloud reporter and inference regressions**

Run:

```powershell
& 'E:\python\python.exe' -m pytest cloud_service/tests/test_status_reporter_lifecycle.py cloud_service/tests/test_packet_review.py -q
```

Expected: all selected tests PASS and V0.1 inference remains unchanged.

- [ ] **Step 5: Commit Task 4**

```powershell
git add -- cloud_edge_project/cloud_service/app.py cloud_edge_project/cloud_service/tests/test_status_reporter_lifecycle.py
git commit -m "feat: report cloud review readiness to scheduler"
```

---

### Task 5: In-process packet cloud-review closed loop and verification

**Files:**
- Create: `cloud_edge_project/verification/conftest.py`
- Create: `cloud_edge_project/verification/test_packet_cloud_review_closed_loop.py`
- Modify only if the test exposes an uncovered approved-design defect: files from Tasks 1-4.

**Interfaces:**
- Consumes: `SchedulerRuntime`, `PacketRoutingBridge`, `CloudReviewService`, real temporary `DeferredCloudRepository`, real temporary `CloudReviewStore`, and controlled cloud client.
- Produces: proof that one low-confidence packet transitions from durable edge storage through scheduler persistence to `SUCCEEDED`, then releases edge raw data.

- [ ] **Step 1: Write the closed-loop integration test**

The test performs this exact sequence with literal identities:

```python
def test_low_confidence_packet_recovers_and_completes_cloud_review(tmp_path):
    # 1. Create a real assigned task in a temporary scheduler database.
    # 2. Persist and route one low-confidence edge completion.
    # 3. With no cloud/link state, assert route is deferred and DB state is PENDING.
    # 4. Register cloud_01 ONLINE/model LOADED and an eligible edge_01->cloud_01 link.
    # 5. Dispatch once through a real CloudReviewService using a controlled cloud client.
    # 6. Report SUCCESS through PacketRoutingService.save_upload_result.
    # 7. Assert repository state SUCCEEDED and review_id review_01.
    # 8. Assert edge store no longer contains packet_01.
```

Do not use an HTTP mock to assert call counts as the outcome. Assert the real SQLite row and real filesystem record.

- [ ] **Step 2: Run the integration test and verify RED if any boundary remains disconnected**

Run:

```powershell
& 'E:\python\python.exe' -m pytest verification/test_packet_cloud_review_closed_loop.py -q
```

Expected before final integration adjustments: FAIL at the first remaining disconnected boundary, with the packet still durable.

- [ ] **Step 3: Add cloud-success/report-failure recovery coverage**

Use a real `CloudReviewStore`, a cloud client that returns `review_01`, and a scheduler reporter that fails once then succeeds. First `handle(control)` must raise from the scheduler report while leaving a `CLOUD_SUCCEEDED` decision checkpoint and the raw packet. The second identical `handle(control)` must report the saved `review_id`, release the raw packet, and leave the cloud client's invocation count at one:

```python
with pytest.raises(ReportUnavailable):
    service.handle(control)
assert store.get_decision(DECISION_ID)["phase"] == "CLOUD_SUCCEEDED"
assert store.get(TASK_ID, BEARING_ID, PACKET_ID) is not None

result = service.handle(control)
assert result["upload_status"] == "SUCCESS"
assert cloud_client.calls == 1
assert store.get(TASK_ID, BEARING_ID, PACKET_ID) is None
```

This test protects the approved rule that scheduler-report failure cannot cause duplicate cloud inference or early raw-data deletion.

- [ ] **Step 4: Make only the minimal integration adjustment**

If RED identifies a mismatch, modify only the approved Tasks 1-4 interfaces. Do not add a second direct upload path, relax route gates, bypass assignment validation, or change device arbitration.

- [ ] **Step 5: Run all focused tests**

Run:

```powershell
& 'E:\python\python.exe' -m pytest scheduler/tests/test_packet_runtime_api.py scheduler/tests/test_deferred_dispatcher_race.py edge_service/verification/test_packet_routing_bridge.py cloud_service/tests/test_status_reporter_lifecycle.py verification/test_packet_cloud_review_closed_loop.py -q
```

Expected: all focused tests PASS with zero failures.

- [ ] **Step 6: Run related regressions**

Run:

```powershell
& 'E:\python\python.exe' -m pytest edge_service/verification/test_bearing_aggregation.py edge_service/verification/test_edge_control_cleanup.py cloud_service/tests/test_packet_review.py sender_module/tests/test_packet_source_mapping.py -q
```

Expected: all runnable related tests PASS. Report unavailable external dependencies precisely.

- [ ] **Step 7: Compile modified packages**

Run:

```powershell
& 'E:\python\python.exe' -m compileall -q scheduler edge_service cloud_service verification
```

Expected: exit code `0`.

- [ ] **Step 8: Attempt the project-wide test suite**

Run:

```powershell
& 'E:\python\python.exe' -m pytest -q
```

Expected: either all tests PASS or the report lists exact collection/runtime blockers such as missing `torch`, MQTT broker, or environment-only services. Do not present a blocked full suite as passing.

- [ ] **Step 9: Audit scope and mutations**

Run:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- cloud_edge_project/scheduler/rule_scheduler.py cloud_edge_project/cloud_service/mock_backend.py
```

Expected: no whitespace errors; no changes to the two protected V0.1 files; unrelated `data/`, `runtime/`, and the pre-existing root-level untracked design file remain untouched.

- [ ] **Step 10: Commit Task 5**

```powershell
git add -- cloud_edge_project/verification
git add --update -- cloud_edge_project/scheduler cloud_edge_project/edge_service cloud_edge_project/cloud_service cloud_edge_project/configs/local.yaml
git commit -m "test: verify packet cloud review closed loop"
```

Before committing, inspect `git diff --cached --name-status` and remove any file outside this plan from the index.
