# Device Arbitration Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scheduler-owned device-level routing for three-bearing summary results, choosing local final arbitration, immediate cloud arbitration, or local provisional arbitration with deferred cloud retry.

**Architecture:** Add a device-level router beside the existing packet router and reuse `CloudNodeRegistry` for cloud and link readiness checks. Keep device-level deferred tasks in a separate SQLite table and dispatcher so packet fields are not forced into device-level contracts.

**Tech Stack:** Python 3, FastAPI-compatible scheduler API, SQLite, pytest, existing `scheduler.cloud_registry` readiness snapshots.

## Global Constraints

- Preserve `POST /scheduler/decide` for sender-to-edge assignment.
- Preserve `POST /scheduler/packet-route` and existing packet-level route names.
- Scheduler remains the control plane and must not carry raw high-sampling data.
- Device-level complexity is exactly `task_complexity = 1 - aggregate_confidence`.
- Cloud load uses only `queue_length` against the configured max queue length.
- Unknown, stale, missing, or unavailable network/cloud state must not be treated as ready.
- Device-level routes are exactly `LOCAL_FINAL`, `CLOUD_ARBITRATION_NOW`, and `LOCAL_PROVISIONAL_AND_DEFER_CLOUD`.
- Current directory is not a Git repository; skip commit steps and report that commits are unavailable.

## File Structure

- Create `cloud_edge_project/scheduler/device_router.py`: validate summary-module device evaluation requests and decide device route.
- Create `cloud_edge_project/scheduler/device_service.py`: join device routing with deferred-device persistence.
- Create `cloud_edge_project/scheduler/deferred_device_repository.py`: SQLite repository for deferred device arbitration tasks.
- Create `cloud_edge_project/scheduler/deferred_device_dispatcher.py`: retry worker that sends device cloud-arbitration control instructions to the summary/result holder.
- Modify `cloud_edge_project/scheduler/routing_config.py`: expose `load_device_arbitration_config()`.
- Modify `cloud_edge_project/scheduler/api.py`: instantiate device router/service/repository/dispatcher and add scheduler endpoints.
- Modify `cloud_edge_project/scheduler/packet_router.py`: add explicit provisional metadata for deferred packet routes.
- Add `cloud_edge_project/tests/test_device_router.py`.
- Add `cloud_edge_project/tests/test_deferred_device_repository.py`.
- Add `cloud_edge_project/tests/test_deferred_device_dispatcher.py`.
- Add `cloud_edge_project/tests/test_scheduler_device_api.py`.

## Task 1: Device Router Core

- [ ] Add failing tests for `LOCAL_FINAL`, `CLOUD_ARBITRATION_NOW`, `LOCAL_PROVISIONAL_AND_DEFER_CLOUD`, bad network/cloud deferral, stable decision id, and complexity mismatch.
- [ ] Implement `DeviceArbitrationRoutingConfig`, route constants, `DeviceArbitrationRouteError`, `DeviceArbitrationRouter.decide()`, `DeviceArbitrationRouter.cloud_delivery_eligibility()`, and `cloud_device_task_id()`.
- [ ] Reuse the packet-router condition logic shape: cloud missing/offline/stale/overloaded/model-not-ready and link missing/unavailable/poor all defer.
- [ ] Validate summary payload fields, duplicate bearing ids, `expected_bearing_count`, `received_bearing_count`, `comparison`, `aggregate_confidence`, and `task_complexity`.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_device_router.py -q`.

## Task 2: Device Service And Deferred Repository

- [ ] Add failing tests for idempotent create, conflicting task identity, atomic claim, retry backoff, restart recovery, expiration, success result idempotency, conflicting result rejection, and absence of raw-data columns.
- [ ] Implement `DeferredDeviceArbitrationRepository` with table `deferred_device_arbitration_task`.
- [ ] Implement `DeviceArbitrationService.route()` to persist any route with `needs_cloud_arbitration=true`.
- [ ] Implement `DeviceArbitrationService.save_arbitration_result()`.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_deferred_device_repository.py -q`.

## Task 3: Device Dispatcher

- [ ] Add failing tests for exact device control payload, eligibility recheck, and dispatch timeout retry.
- [ ] Implement `SummaryDispatchClient` posting to `/summary/cloud-device-arbitration-tasks`.
- [ ] Implement `DeferredDeviceArbitrationDispatcher` with `dispatch_once()`, `start()`, `stop()`, and background loop.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_deferred_device_dispatcher.py -q`.

## Task 4: Scheduler API Integration

- [ ] Add failing tests proving scheduler exposes `route_device_arbitration()`, `save_device_cloud_arbitration_result()`, `/scheduler/device-arbitration-route`, and `/scheduler/device-cloud-arbitration-results`.
- [ ] Add `load_device_arbitration_config()` in `routing_config.py`.
- [ ] Instantiate device router, service, repository, and dispatcher in `api.py`.
- [ ] Add FastAPI routes and HTTP fallback handlers.
- [ ] Extend `_error_payload()` for device route and deferred-device errors.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_scheduler_device_api.py -q`.

## Task 5: Packet Provisional Metadata

- [ ] Extend existing packet-router tests to assert `result_instruction` for direct and deferred packet routes.
- [ ] Add `result_instruction` in `PacketRouter._response()`.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_packet_router.py -q`.

## Task 6: Final Verification

- [ ] Run focused scheduler tests:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_device_router.py `
  tests/test_deferred_device_repository.py `
  tests/test_deferred_device_dispatcher.py `
  tests/test_scheduler_device_api.py `
  tests/test_packet_router.py `
  tests/test_scheduler_packet_api.py `
  tests/test_packet_cloud_integration.py `
  -q
```

- [ ] Run compile check:

```powershell
.\.venv\Scripts\python.exe -m compileall scheduler tests
```

- [ ] Report that commits are unavailable because this checkout is not a Git repository.
