# Package Cloud Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add document-compliant packet-level cloud routing, cloud-node telemetry, deferred cloud retry, edge raw-packet persistence, and edge-to-scheduler upload reporting without changing the existing assignment or cloud acknowledgement contracts.

**Architecture:** Keep `/scheduler/decide` and its repository untouched. Add independent scheduler registries for cloud status and edge-to-cloud links, a pure packet router, and a SQLite deferred-job repository/dispatcher. Add an edge-owned disk store and upload service that calls the existing `/cloud/infer`; add a cloud background reporter that only posts node status. Wire all new endpoints through both scheduler HTTP implementations where applicable.

**Tech Stack:** Python 3.12-compatible code, FastAPI, `requests`, SQLite, pytest.

**Execution note:** This checkout is not a Git worktree, so the commit steps normally required by the planning skill are omitted. Each task still follows red-green-refactor and is verified before moving on.

---

## Task 1: Cloud status and edge-cloud link registries

**Files:**
- Create: `cloud_edge_project/scheduler/cloud_registry.py`
- Modify: `cloud_edge_project/scheduler/node_registry.py`
- Create: `cloud_edge_project/tests/test_scheduler_cloud_registry.py`

- [ ] Write literal-fixture tests for accepting all document 6.3 fields, status-message idempotency/conflict, stale status, queue/model eligibility, and rejecting malformed enums/timestamps.
- [ ] Write compatibility tests proving the existing sender-to-edge link payload still works and document 6.4 edge-to-cloud snapshots are stored separately.
- [ ] Run `python -m pytest cloud_edge_project/tests/test_scheduler_cloud_registry.py -q` and confirm imports/behavior fail for the missing feature.
- [ ] Implement `CloudNodeRegistry` plus immutable cloud status and cloud-link snapshots. Route only on health, queue, model readiness, freshness, and the 6.4 link metrics; retain all other telemetry without using it as load.
- [ ] Extend `NodeRegistry.update_link()` by schema discrimination while preserving its legacy response and lookup behavior.
- [ ] Re-run the focused tests and refactor only after green.

## Task 2: Packet-result validation and route matrix

**Files:**
- Create: `cloud_edge_project/scheduler/packet_router.py`
- Modify: `cloud_edge_project/scheduler/assignment_scheduler.py`
- Create: `cloud_edge_project/tests/test_packet_router.py`

- [ ] Add tests for document 6.5 identity binding, assignment ownership, sequence range 1..80, status/error/output combinations, confidence range, and `task_complexity == 1 - confidence` within `1e-6`.
- [ ] Add route-matrix tests for `DIRECT_FINAL_TO_SUMMARY`, `CLOUD_REVIEW_NOW`, and `EDGE_PROVISIONAL_AND_DEFER_CLOUD`, including each stale/missing/offline/overloaded/model/network failure reason.
- [ ] Add the boundary test that confidence `0.80` routes directly even when cloud and link state are absent.
- [ ] Run `python -m pytest cloud_edge_project/tests/test_packet_router.py -q` and confirm the new contracts fail before implementation.
- [ ] Implement a pure `PacketRouter` with defaults: confidence `0.80`, queue `5`, cloud-status TTL `5s`, goodput `2.0Mbps`, RTT p95 `100ms`, and loss `0.10`.
- [ ] Add a read-only assignment identity accessor to `AssignmentScheduler`/repository only where required; do not change `/scheduler/decide` validation, selection, response, ACK, or retry behavior.
- [ ] Return the document 7.2 shape with consistent booleans, nullable inapplicable targets, deterministic reason codes, and generated stable decision/cloud-task identifiers.
- [ ] Re-run focused tests.

## Task 3: Deferred cloud-task persistence and state transitions

**Files:**
- Create: `cloud_edge_project/scheduler/deferred_cloud_repository.py`
- Create: `cloud_edge_project/tests/test_deferred_cloud_repository.py`

- [ ] Add SQLite-backed tests for atomic create/idempotent replay, conflicting decision identity, claim leasing, restart recovery, upload-result idempotency, and terminal state retention.
- [ ] Add literal schedule tests proving retries occur after 5, 10, 20, 40, 60 seconds and every 60 seconds thereafter.
- [ ] Add expiration tests proving non-terminal work becomes `EXPIRED` after 24 hours while its lightweight audit row remains.
- [ ] Run the focused test file and confirm RED.
- [ ] Implement schema initialization and state transitions `PENDING -> DISPATCHING -> WAITING_RESULT -> SUCCEEDED`, retryable rollback to `PENDING`, plus `PERMANENT_FAILED` and `EXPIRED`.
- [ ] Ensure scheduler rows contain references only and never persist raw sample `values`.
- [ ] Re-run focused tests.

## Task 4: Scheduler APIs and deferred dispatcher

**Files:**
- Create: `cloud_edge_project/scheduler/deferred_dispatcher.py`
- Modify: `cloud_edge_project/scheduler/api.py`
- Modify: `cloud_edge_project/scheduler/__init__.py`
- Create: `cloud_edge_project/tests/test_scheduler_packet_api.py`

- [ ] Add API-level tests for `POST /scheduler/packet-route`, `/scheduler/cloud-nodes/status`, and `/scheduler/cloud-upload-results`, including 400/409/503 error mapping and idempotent duplicate responses.
- [ ] Add a dispatcher test using a real repository and a narrow fake HTTP transport: due work posts the exact document 7.3 payload to `/edge/cloud-review-tasks`; timeout preserves the same decision, holder, and target for retry.
- [ ] Add compatibility tests for both FastAPI handlers and `SchedulerRequestHandler` route maps.
- [ ] Run the focused API tests and confirm RED.
- [ ] Wire registries, router, repository, and upload-result handler. Persist deferred work transactionally before returning a deferred route.
- [ ] Implement the background dispatcher with one claim lease per decision and restart recovery; do not send raw data through the scheduler.
- [ ] Start/stop all scheduler monitors and workers cleanly through `atexit` while retaining direct-script operation.
- [ ] Re-run focused tests.

## Task 5: Edge raw-packet disk store and cloud upload service

**Files:**
- Create: `cloud_edge_project/edge_service/src/cloud_review/config.py`
- Create: `cloud_edge_project/edge_service/src/cloud_review/contracts.py`
- Create: `cloud_edge_project/edge_service/src/cloud_review/store.py`
- Create: `cloud_edge_project/edge_service/src/cloud_review/service.py`
- Create: `cloud_edge_project/edge_service/src/cloud_review/__init__.py`
- Modify: `cloud_edge_project/edge_service/app.py`
- Create: `cloud_edge_project/edge_service/tests/unit/test_cloud_review.py`
- Create: `cloud_edge_project/edge_service/tests/unit/test_cloud_review_http.py`

- [ ] Add real-filesystem tests for atomic storage keyed by `task_id + bearing_id + packet_id`, restart recovery, successful release, permanent-failure release, and 24-hour expiry.
- [ ] Add service tests proving document 7.3 validation, raw-reference identity checking, calls to the unchanged `/cloud/infer` contract, `review_id` correlation, and correct retryable/permanent status mapping.
- [ ] Add HTTP tests for `POST /edge/cloud-review-tasks` idempotency and conflicts. Mock only external HTTP calls; keep disk and service behavior real.
- [ ] Run the focused edge tests and confirm RED.
- [ ] Implement atomic JSON/NPZ-safe persistence without placing high-rate samples in scheduler storage. Reuse the validated cached raw packet when the immediate route is selected, and require persistence before acknowledging an executable cloud route.
- [ ] Implement the cloud client and scheduler result reporter. Delete the raw packet only after cloud success has also been successfully reported to the scheduler; retain it after ambiguous/reporting failures.
- [ ] Expose `/edge/cloud-review-tasks` without changing `/edge/infer` or `/edge/tasks` response contracts.
- [ ] Re-run focused tests.

## Task 6: Cloud node-status reporter

**Files:**
- Create: `cloud_edge_project/cloud_service/status_reporter.py`
- Modify: `cloud_edge_project/cloud_service/config.py`
- Modify: `cloud_edge_project/cloud_service/app.py`
- Create: `cloud_edge_project/tests/test_cloud_status_reporter.py`

- [ ] Add payload tests covering every document 6.3 field and proving `queue_length` plus model status reflect the current cloud process.
- [ ] Add loop tests proving a report is attempted every second and a failed report is swallowed until the next cycle without disabling `/cloud/infer`.
- [ ] Run the focused tests and confirm RED.
- [ ] Implement a reporter client with scheduler URL/timeout settings, unique status-message IDs, nanosecond timestamps, health/resource/model/network fields, and no task ACK callback.
- [ ] Start and cancel the reporter in the existing FastAPI lifespan alongside the expiry worker.
- [ ] Re-run focused tests.

## Task 7: Configuration and operator documentation

**Files:**
- Modify: `cloud_edge_project/config.yaml`
- Modify: `cloud_edge_project/scheduler/README.md`
- Modify: `cloud_edge_project/README.md`

- [ ] Add the confirmed packet-routing, cloud-node, cloud-network, persistence, and endpoint defaults while keeping environment overrides for service URLs and database/cache locations.
- [ ] Document sender/edge/cloud/network caller-to-receiver ownership and explicitly state that cloud confirmations remain `/cloud/infer`, `/cloud/raw-context-batches`, and `/cloud/edge-feature-summaries` responses.
- [ ] Document that direct-final output targets the future summary module contract but does not implement the one-second window in this change.
- [ ] Check examples against live validators; do not test prose by source-text matching.

## Task 8: Regression and end-to-end contract verification

**Files:**
- Modify only if a real regression requires a scoped fix.

- [ ] Run all new scheduler tests together.
- [ ] Run all edge unit tests, including the new cloud-review tests.
- [ ] Run existing project tests and record any pre-existing dependency/infrastructure limitation separately from product failures.
- [ ] Run an integration contract test with temporary SQLite/cache directories: seed an assigned task, report cloud/link state, submit a low-confidence packet, upload through a fake cloud boundary, report success, and verify scheduler `SUCCEEDED` plus raw-file removal.
- [ ] Re-run the same flow with poor network, verify deferred persistence, restore the link, dispatch the same decision ID, and verify completion.
- [ ] Inspect the resulting scheduler SQLite schema/data to confirm no raw `values` were stored.
- [ ] Apply `superpowers:verification-before-completion`; report only commands freshly observed passing.
