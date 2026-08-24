# Frontend Realtime Records Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Sender rounds use incrementing device IDs and make the dashboard browse real database records with unambiguous live counters and link timestamps.

**Architecture:** Keep identity generation in the Sender CLI, add narrowly scoped read methods to the three existing SQLite repositories/services, and expose three bounded GET endpoints from the Cloud FastAPI app. The static frontend continues using the existing gateway and `Api` helper; it polls the new endpoints and renders persisted JSON without introducing a frontend framework.

**Tech Stack:** Python 3.11+, dataclasses, SQLite, FastAPI, pytest, vanilla HTML/CSS/JavaScript, SSE.

## Global Constraints

- One `--rounds` iteration represents one device; its three configured bearings share the same generated device ID.
- `machine_01` is the first round and `machine_02` is the second round in the same process.
- Device decisions and conflict arbitrations must remain separate concepts in API names and UI labels.
- List limits are integers from 1 through 200 and results are newest first.
- Do not create fake arbitration rows or modify existing runtime records.
- The total-dashboard live counters must not restore old `localStorage` stream records.
- Link timestamps come from backend nanosecond fields; do not synthesize state-start times.

---

## File Map

- `cloud_edge_project/sender_module/sender/__main__.py`: derive per-round `SenderConfig` values.
- `cloud_edge_project/sender_module/tests/test_rounds.py`: prove one round shares one ID and later rounds increment it.
- `cloud_edge_project/cloud_service/task_results.py`: list recent persisted device decisions.
- `cloud_edge_project/cloud_service/device_arbitration/repository.py`: list recent persisted conflict arbitrations.
- `cloud_edge_project/cloud_service/global_analysis/result_repository.py`: list recent global analysis results across devices.
- `cloud_edge_project/cloud_service/app.py`: validate query parameters and expose the three read endpoints.
- `cloud_edge_project/cloud_service/tests/test_recent_record_queries.py`: repository and API regression coverage.
- `cloud_edge_project/frontend/index.html`: separate the current page session from old browser cache.
- `cloud_edge_project/frontend/arbitration.html`: automatically list device decisions and true conflict arbitrations.
- `cloud_edge_project/frontend/analysis.html`: automatically list recent saved analyses.
- `cloud_edge_project/frontend/topology.html`: render actual link state and timestamps.
- `cloud_edge_project/tests/test_frontend_realtime_views.py`: static contract checks for the framework-free pages.

### Task 1: Increment Sender device IDs by round

**Files:**
- Modify: `cloud_edge_project/sender_module/tests/test_rounds.py`
- Modify: `cloud_edge_project/sender_module/sender/__main__.py`

**Interfaces:**
- Produces: `device_id_for_round(base_device_id: str, round_number: int, total_rounds: int) -> str`
- Consumes: immutable `SenderConfig`; use `dataclasses.replace(config, device_id=...)`.

- [ ] **Step 1: Write failing round identity tests**

Add tests that call the wished-for helper and make `test_formal_round_count_runs_45_sender_tasks` capture `config.device_id`:

```python
from types import SimpleNamespace

def test_device_id_increments_and_preserves_suffix_width() -> None:
    assert cli.device_id_for_round("machine_01", 1, 2) == "machine_01"
    assert cli.device_id_for_round("machine_01", 2, 2) == "machine_02"
    assert cli.device_id_for_round("machine_099", 2, 2) == "machine_100"

def test_multiple_rounds_require_a_numeric_suffix() -> None:
    with pytest.raises(ValueError, match="numeric suffix"):
        cli.device_id_for_round("machine", 1, 2)
```

Change the existing test config to `SimpleNamespace(device_id="machine_01")` and assert captured IDs equal fifteen groups from `machine_01` through `machine_15`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest cloud_edge_project/sender_module/tests/test_rounds.py -q
```

Expected: failure because `device_id_for_round` does not exist and the current loop passes the unchanged config.

- [ ] **Step 3: Implement the minimal round identity function**

In `sender/__main__.py`, import `replace` and `re`, then implement:

```python
def device_id_for_round(base_device_id: str, round_number: int, total_rounds: int) -> str:
    if total_rounds == 1:
        return base_device_id
    match = re.fullmatch(r"(.*?)(\d+)", base_device_id)
    if match is None:
        raise ValueError("device_id must end with a numeric suffix when --rounds is greater than 1")
    prefix, suffix = match.groups()
    value = int(suffix) + round_number - 1
    return prefix + str(value).zfill(len(suffix))
```

Inside the loop, create `round_config = replace(config, device_id=device_id_for_round(...))` and pass it to `run_all_senders`.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same pytest command. Expected: all tests in `test_rounds.py` pass.

### Task 2: Add bounded recent-record queries

**Files:**
- Create: `cloud_edge_project/cloud_service/tests/test_recent_record_queries.py`
- Modify: `cloud_edge_project/cloud_service/task_results.py`
- Modify: `cloud_edge_project/cloud_service/device_arbitration/repository.py`
- Modify: `cloud_edge_project/cloud_service/global_analysis/result_repository.py`

**Interfaces:**
- Produces: `TaskResultService.list_recent_device_decisions(device_id: str | None, limit: int) -> list[dict[str, Any]]`
- Produces: `DeviceArbitrationRepository.list_recent(device_id: str | None, limit: int) -> list[dict[str, Any]]`
- Produces: `GlobalAnalysisResultRepository.list_recent(scenario_type: str, subject_id: str | None, limit: int) -> list[dict[str, Any]]`

- [ ] **Step 1: Write failing repository tests**

Create temporary databases, insert two JSON rows with different devices/times using existing ingest/save APIs or `connect`, then assert:

```python
assert [row["device_id"] for row in service.list_recent_device_decisions(None, 10)] == ["machine_02", "machine_01"]
assert [row["device_id"] for row in service.list_recent_device_decisions("machine_01", 10)] == ["machine_01"]
assert arbitration_repository.list_recent(None, 10) == []
assert [row["subject_id"] for row in analysis_repository.list_recent("bearing", None, 10)] == ["machine_02", "machine_01"]
```

Use real persisted JSON as the asserted return value; do not assert a hand-built summary projection.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest cloud_edge_project/cloud_service/tests/test_recent_record_queries.py -q
```

Expected: missing-method failures.

- [ ] **Step 3: Implement minimal SQL queries**

Each method chooses one of two parameterized SQL statements depending on whether the optional device/subject filter is supplied. Select only `payload_json` or `result_json`, order by the persisted nanosecond column descending with the stable ID descending as a tie-breaker, apply `LIMIT ?`, and `json.loads` every row.

For decisions use `received_at_ns, result_id`; for arbitration use `created_at_ns, arbitration_id`; for analysis use `created_at_ns, analysis_id`.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 2 pytest command. Expected: all repository cases pass, including the empty arbitration list.

### Task 3: Expose recent-record APIs

**Files:**
- Modify: `cloud_edge_project/cloud_service/tests/test_recent_record_queries.py`
- Modify: `cloud_edge_project/cloud_service/app.py`

**Interfaces:**
- Produces: `GET /cloud/device-decision-results/recent`
- Produces: `GET /cloud/device-arbitration/recent`
- Produces: `GET /cloud/global-analysis/recent`
- Produces: `_recent_limit(value: int) -> int`, accepting 1 through 200.

- [ ] **Step 1: Write failing route tests**

Monkeypatch `load_cloud_settings` to return an object whose `database_path` points at the populated temporary database. Call route functions directly and assert:

```python
response = cloud_api.list_recent_device_decisions(device_id="machine_01", limit=20)
assert response == {"success": True, "items": [expected_decision], "count": 1}

response = cloud_api.list_recent_device_arbitrations(device_id=None, limit=20)
assert response == {"success": True, "items": [], "count": 0}

invalid = cloud_api.list_recent_global_analyses(scenario_type="bearing", subject_id=None, limit=0)
assert invalid.status_code == 400
assert json.loads(invalid.body)["error_code"] == "INVALID_RECENT_LIMIT"
```

- [ ] **Step 2: Run tests and verify RED**

Run the Task 2 pytest command again. Expected: missing route/helper failures.

- [ ] **Step 3: Implement the three routes**

Add static `/recent` routes before `/cloud/device-arbitration/{conflict_id}` and before global-analysis dynamic routes where needed. Validate limits explicitly, strip empty optional filters to `None`, call the repository/service methods, and return the uniform envelope. Catch `sqlite3.Error` and return HTTP 503 with `SERVICE_UNAVAILABLE`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```powershell
python -m pytest cloud_edge_project/cloud_service/tests/test_recent_record_queries.py cloud_edge_project/cloud_service/tests/test_v12_result_receiver.py cloud_edge_project/cloud_service/tests/test_global_analysis_service.py -q
```

Expected: all selected Cloud tests pass.

### Task 4: Render live session counts and persisted record lists

**Files:**
- Create: `cloud_edge_project/tests/test_frontend_realtime_views.py`
- Modify: `cloud_edge_project/frontend/index.html`
- Modify: `cloud_edge_project/frontend/arbitration.html`
- Modify: `cloud_edge_project/frontend/analysis.html`
- Modify: `cloud_edge_project/frontend/topology.html`

**Interfaces:**
- Consumes: the three API envelopes from Task 3.
- Consumes: network link fields `current_state`, `available`, `state_since_ns`, `desired_parameters`, and `applied_parameters`.
- Produces: `fmtNs(timestampNs)` in pages that display backend nanoseconds.

- [ ] **Step 1: Write failing static frontend contract tests**

Read the four HTML files as UTF-8 and assert visible/data contract markers:

```python
def test_dashboard_does_not_restore_old_streams_into_live_counts() -> None:
    page = _page("index.html")
    assert "loadStore()" not in page
    assert "本次页面会话" in page

def test_arbitration_page_lists_decisions_and_arbitrations() -> None:
    page = _page("arbitration.html")
    assert "device-decision-results/recent" in page
    assert "device-arbitration/recent" in page
    assert "暂无冲突仲裁记录" in page

def test_analysis_page_lists_recent_saved_results() -> None:
    assert "global-analysis/recent" in _page("analysis.html")

def test_topology_uses_backend_state_timestamp() -> None:
    page = _page("topology.html")
    assert "state_since_ns" in page
    assert "current_state" in page
    assert "刷新时间" in page
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest cloud_edge_project/tests/test_frontend_realtime_views.py -q
```

Expected: assertions fail against the current pages.

- [ ] **Step 3: Stop restoring cached streams on the dashboard**

Initialize live `stats`, series, and `streamData` from empty constants rather than `loadStore()`. Remove automatic `saveStore` calls and change the packet metric subtitle/help text to “本次页面会话”. Keep SSE ingestion and bounded in-memory arrays unchanged.

- [ ] **Step 4: Add automatic decision/arbitration lists**

In `arbitration.html`, add a device filter, two list containers, and a shared five-second refresh. Render decision rows from their actual fields (`result_id`, `device_id`, `task_id`, `final_state`, `has_conflict`, `confidence`, `closed_at_ns`/`created_at_ns`). Render arbitration rows from (`arbitration_id`, `conflict_id`, `subject_id`/`device_id`, `final_action`, `confidence`, `created_at_ns`). Clicking a row must render the stored JSON with the existing details helper.

- [ ] **Step 5: Add automatic global-analysis history**

In `analysis.html`, add a recent-results list fetched on load and every five seconds. Use `subject_id`, `created_at_ns`, `analysis_window.actual_task_count`, `device_health_analysis.latest_state`, and `device_health_analysis.trend`; clicking a row calls `renderResult({result: item}, ...)`.

- [ ] **Step 6: Align topology state and timestamps**

In `topology.html`, derive warning state from `current_state !== "normal" || available === false`, display state label, display the formatted `state_since_ns`, and update a “刷新时间” hint after a successful fetch. Stop reading nonexistent `status`, `toxics`, and `active_toxics` fields.

- [ ] **Step 7: Run frontend contract tests and verify GREEN**

Run the Task 4 pytest command. Expected: all four static checks pass.

### Task 5: Regression verification and handoff

**Files:**
- Modify only files already changed if verification exposes a defect.

**Interfaces:**
- Verifies all interfaces from Tasks 1 through 4.

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest cloud_edge_project/sender_module/tests/test_rounds.py cloud_edge_project/cloud_service/tests/test_recent_record_queries.py cloud_edge_project/cloud_service/tests/test_v12_result_receiver.py cloud_edge_project/cloud_service/tests/test_global_analysis_service.py cloud_edge_project/tests/test_frontend_realtime_views.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run broader related suites**

```powershell
python -m pytest cloud_edge_project/sender_module/tests cloud_edge_project/cloud_service/tests/test_global_analysis_*.py cloud_edge_project/cloud_service/tests/test_v12_*.py -q
```

Expected: zero failures.

- [ ] **Step 3: Perform syntax and diff checks**

```powershell
python -m compileall -q cloud_edge_project/sender_module/sender cloud_edge_project/cloud_service
git diff --check
git status --short
```

Expected: compile and whitespace checks exit 0; status contains only the planned files.

- [ ] **Step 4: Verify against the live database without writing it**

Start the existing backend/frontend stack if already configured, open the pages through `http://127.0.0.1:8088`, and confirm the existing database returns device decisions/global analyses while the arbitration list accurately reports empty. Do not insert sample rows.

- [ ] **Step 5: Report exact evidence**

Report test counts, changed files, the no-fake-arbitration boundary, the per-process round numbering rule, and any live service that was unavailable during validation.
