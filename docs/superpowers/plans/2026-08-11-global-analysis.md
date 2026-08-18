# Global Analysis Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the bearing scenario's read-only global-analysis workflow and `global_analysis_result/2.0` contract.

**Architecture:** The service depends on a normalized data-source protocol and composes pure analyzers. SQLite concerns stay in the bearing data source; metrics stay in focused public or bearing-specific analyzers. The repository persists complete results and maintains existing query columns.

**Tech Stack:** Python 3, SQLite, FastAPI, pytest.

## Global Constraints

- Read historical structured results only; never create or alter upstream diagnosis, aggregation, review, or arbitration records.
- Analyzer signatures use rows plus `GlobalAnalysisConfig` and never execute SQL.
- Empty denominators yield `null` rates and an explicit analysis status, never fabricated zero metrics.
- Preserve the existing POST and latest-result API routes.

---

### Task 1: Shared contracts and data sources

**Files:**
- Create: `cloud_edge_project/cloud_service/global_analysis/contracts.py`
- Create: `cloud_edge_project/cloud_service/global_analysis/common.py`
- Create: `cloud_edge_project/scenarios/bearing/cloud/global_analysis/data_source.py`
- Modify: `cloud_edge_project/scenarios/bearing/cloud/global_analysis/config.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_contracts.py`

**Interfaces:** Produce `GlobalAnalysisConfig`, `GlobalAnalysisDataSource`, `FakeGlobalAnalysisDataSource`, severity helpers and status helpers.

- [ ] **Step 1: Write the failing test**

```python
def test_fake_source_filters_related_rows_to_the_limited_task_window():
    source = FakeGlobalAnalysisDataSource(
        device_tasks=[{"task_id": "t1"}, {"task_id": "t2"}],
        bearing_tasks=[{"task_id": "t1"}],
    )
    assert source.load("machine_01", 1)["bearing_tasks"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_service/tests/test_global_analysis_contracts.py -v`
Expected: FAIL because `data_source` does not exist.

- [ ] **Step 3: Implement the minimal contract**

```python
class GlobalAnalysisDataSource(Protocol):
    def load(self, device_id: str, task_limit: int) -> dict[str, list[dict[str, Any]]]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest cloud_service/tests/test_global_analysis_contracts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud_service/global_analysis scenarios/bearing/cloud/global_analysis cloud_service/tests/test_global_analysis_contracts.py
git commit -m "feat: add global analysis contracts"
```

### Task 2: Device health and bearing risk

**Files:**
- Create: `cloud_edge_project/cloud_service/global_analysis/device_health_analyzer.py`
- Create: `cloud_edge_project/scenarios/bearing/cloud/global_analysis/bearing_risk_analyzer.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_device_health.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_bearing_risk.py`

**Interfaces:** Produce `analyze_device_health(rows, config)` and `analyze_bearing_risk(rows, config)`.

- [ ] **Step 1: Write failing tests**

```python
def test_device_health_reports_recent_risk_and_degrading_trend():
    result = analyze_device_health(rows_with_states("normal", "normal", "warning", "abnormal", "abnormal"), config)
    assert result["recent_risk_rate"] == 1.0
    assert result["trend"] == "degrading"

def test_bearing_risk_selects_primary_and_detects_multi_bearing_degradation():
    result = analyze_bearing_risk(rows, config)
    assert result["primary_risk_bearing_id"] == "bearing_02"
    assert result["multi_bearing_degradation"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest cloud_service/tests/test_global_analysis_device_health.py cloud_service/tests/test_global_analysis_bearing_risk.py -v`
Expected: FAIL because analyzers do not exist.

- [ ] **Step 3: Implement minimal analyzers**

```python
def analyze_device_health(rows, config):
    # counts, rates, recent risk, consecutive abnormal, two-window trend

def analyze_bearing_risk(rows, config):
    # group dynamically by bearing_id and analyze bearing_state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest cloud_service/tests/test_global_analysis_device_health.py cloud_service/tests/test_global_analysis_bearing_risk.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud_service/global_analysis/device_health_analyzer.py scenarios/bearing/cloud/global_analysis/bearing_risk_analyzer.py cloud_service/tests/test_global_analysis_device_health.py cloud_service/tests/test_global_analysis_bearing_risk.py
git commit -m "feat: analyze device and bearing risk"
```

### Task 3: Packet and bearing-aggregation performance

**Files:**
- Create: `cloud_edge_project/cloud_service/global_analysis/packet_model_analyzer.py`
- Create: `cloud_edge_project/scenarios/bearing/cloud/global_analysis/bearing_aggregation_analyzer.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_model_performance.py`

**Interfaces:** Produce detailed reviewed-pair metrics, directional errors, version groups, condition buckets, and optional trigger analysis.

- [ ] **Step 1: Write failing tests**

```python
def test_packet_analysis_reports_correction_directions_versions_and_high_load_weakness():
    result = analyze_packet_model(rows, config)
    assert result["risk_underestimation_count"] == 1
    assert result["risk_overestimation_count"] == 1
    assert result["condition_weakness"]["bucket"] == "high"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_service/tests/test_global_analysis_model_performance.py -v`
Expected: FAIL because analyzer modules do not exist.

- [ ] **Step 3: Implement minimal performance analyzers**

```python
def analyze_packet_model(rows, config):
    # reviewed-only base metrics, by edge model version, then condition buckets

def analyze_bearing_aggregation(rows, config):
    # reviewed-only base metrics, aggregation version and optional trigger groups
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest cloud_service/tests/test_global_analysis_model_performance.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud_service/global_analysis/packet_model_analyzer.py scenarios/bearing/cloud/global_analysis/bearing_aggregation_analyzer.py cloud_service/tests/test_global_analysis_model_performance.py
git commit -m "feat: analyze reviewed model performance"
```

### Task 4: Arbitration analysis and problem detection

**Files:**
- Create: `cloud_edge_project/cloud_service/global_analysis/arbitration_analyzer.py`
- Create: `cloud_edge_project/cloud_service/global_analysis/problem_detector.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_problems.py`

**Interfaces:** Produce arbitration target metrics and threshold-qualified, persistent problem candidates.

- [ ] **Step 1: Write failing tests**

```python
def test_arbitration_uses_device_rows_for_conflict_and_null_for_no_arbitrations():
    result = analyze_device_arbitration(device_rows, [], config)
    assert result["conflict_rate"] == 0.05
    assert result["arbitration_success_rate"] is None

def test_repeated_problem_candidate_becomes_persistent():
    assert detect_problem_candidates(...)[0]["persistence"] == "persistent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_service/tests/test_global_analysis_problems.py -v`
Expected: FAIL because modules do not exist.

- [ ] **Step 3: Implement configured target and persistence rules**

```python
def analyze_device_arbitration(device_rows, arbitration_rows, config): ...
def detect_problem_candidates(*, device_health, bearing_risk, packet_diagnosis, bearing_aggregation, device_arbitration, previous_analysis, config): ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest cloud_service/tests/test_global_analysis_problems.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud_service/global_analysis/arbitration_analyzer.py cloud_service/global_analysis/problem_detector.py cloud_service/tests/test_global_analysis_problems.py
git commit -m "feat: detect global analysis problems"
```

### Task 5: SQLite integration, v2 service, persistence, and API

**Files:**
- Modify: `cloud_edge_project/scenarios/bearing/cloud/global_analysis/data_loader.py`
- Modify: `cloud_edge_project/cloud_service/global_analysis/service.py`
- Modify: `cloud_edge_project/cloud_service/global_analysis/result_repository.py`
- Modify: `cloud_edge_project/cloud_service/app.py`
- Test: `cloud_edge_project/cloud_service/tests/test_global_analysis_service.py`

**Interfaces:** Produce stored and retrievable `global_analysis_result/2.0` without upstream writes.

- [ ] **Step 1: Write the failing integration test**

```python
def test_service_persists_v2_result_and_latest_query(tmp_path):
    result = GlobalAnalysisService(tmp_path / "cloud.db", data_source=source).analyze("bearing", "machine_01", 20)
    assert result["schema_version"] == "global_analysis_result/2.0"
    assert result["packet_diagnosis_analysis"]["status"] == "not_available"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest cloud_service/tests/test_global_analysis_service.py -v`
Expected: FAIL because the service neither accepts a source nor returns v2.

- [ ] **Step 3: Implement only the orchestration and storage changes**

```python
class GlobalAnalysisService:
    def __init__(self, database_path: Path, data_source: GlobalAnalysisDataSource | None = None): ...
    def analyze(self, scenario_type: str, subject_id: str, task_limit: int = 20) -> dict[str, Any]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest cloud_service/tests/test_global_analysis_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scenarios/bearing/cloud/global_analysis/data_loader.py cloud_service/global_analysis/service.py cloud_service/global_analysis/result_repository.py cloud_service/app.py cloud_service/tests/test_global_analysis_service.py
git commit -m "feat: integrate global analysis workflow"
```

### Task 6: Full verification

**Files:** Modify only if a verification failure exposes a defect in Tasks 1-5.

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest cloud_service/tests/test_global_analysis_*.py -v`
Expected: all global-analysis tests pass.

- [ ] **Step 2: Run cloud-service tests**

Run: `python -m pytest cloud_service/tests -v`
Expected: all tests pass.

- [ ] **Step 3: Inspect final diff**

Run: `git diff main...HEAD --check && git status --short`
Expected: no whitespace errors and only planned changes.
