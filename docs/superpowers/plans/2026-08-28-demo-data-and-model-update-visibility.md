# Demo Data And Model Update Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent consecutive demo devices from being rejected by Edge and make the model-update panel explain why no update task was triggered.

**Architecture:** Allow one sender cache to retain multiple device/task histories instead of binding it to the first device for the whole retention window; context queries filter by device and bearing so histories are not mixed. Keep model-update decisions evidence-based; when the API returns no tasks, derive a truthful eligibility message from the latest global-analysis result instead of fabricating an update recommendation.

**Tech Stack:** Python 3, pytest, vanilla JavaScript, local HTTP gateway.

## Global Constraints

- Preserve existing user changes and modify only the validation cache, focused tests, and model-update empty-state presentation.
- A device fault alone is not evidence that the model needs updating.
- Do not weaken the 60-second raw-cache retention policy.
- Do not commit or push automatically.

---

### Task 1: Isolate Edge validation cache by task

**Files:**
- Create: `cloud_edge_project/edge_service/verification/test_validation_cache_task_isolation.py`
- Modify: `cloud_edge_project/edge_service/src/edge_validation_cache/manager.py`

**Interfaces:**
- Consumes: packet identity fields `sender_id`, `task_id`, `device_id`, and `bearing_id`.
- Produces: task-isolated cache lookup while retaining `RawPacketRef = (sender_id, task_id, sequence_number)`.

- [ ] **Step 1: Write the failing test**

Create two valid packets with the same sender and different task/device identities inside the 60-second retention period. Assert that both calls to `EdgeValidationCache.process` succeed and both raw references remain readable.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_edge_project/edge_service/verification/test_validation_cache_task_isolation.py -q`

Expected: the second packet fails with `RAW_CACHE_WRITE_FAILED` caused by `sender_binding`.

- [ ] **Step 3: Write minimal implementation**

Remove the stale sender-to-device rejection and filter context lookup to the requested device and bearing. Do not change raw-reference lookup, packet validation, capacity, pinning, or retention rules.

- [ ] **Step 4: Run test to verify it passes**

Run the same focused pytest command. Expected: PASS.

### Task 2: Explain an empty model-update panel

**Files:**
- Modify: `cloud_edge_project/frontend/index.html`
- Create: `cloud_edge_project/frontend/verification/test_model_update_empty_state.py`

**Interfaces:**
- Consumes: `GET /api/cloud/cloud/model-update/recent` and `GET /api/cloud/cloud/global-analysis/recent?limit=1`.
- Produces: a visible reason when no update task exists: no model-bias candidate, insufficient reviewed evidence, or analysis not yet available.

- [ ] **Step 1: Write the failing test**

Assert the page contains the global-analysis fallback request and distinct text for “no model deviation evidence” rather than only “no model update task”.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_edge_project/frontend/verification/test_model_update_empty_state.py -q`

Expected: FAIL because the current renderer only emits `暂无云端模型更新任务`.

- [ ] **Step 3: Write minimal implementation**

When the recent-update list is empty, fetch the latest global analysis. Render an evidence-bound message and keep the list empty. Do not create a model-update task from device fault labels alone.

- [ ] **Step 4: Run test to verify it passes**

Run the focused frontend verification test. Expected: PASS.

### Task 3: Verify the integrated result

**Files:**
- Verify only; no additional production files.

**Interfaces:**
- Consumes: both focused changes.
- Produces: fresh test and HTTP evidence.

- [ ] **Step 1: Run related suites**

Run the two focused tests plus existing Cloud model-update/global-analysis tests and Edge verification tests touching validation cache behavior.

- [ ] **Step 2: Verify live routes**

Confirm the Cloud recent-update and global-analysis endpoints return HTTP 200 through both port 8004 and the frontend gateway.

- [ ] **Step 3: Review the diff**

Confirm only directly related files changed and report that a fresh demo rerun is required because packets rejected in the previous run cannot be reconstructed.
