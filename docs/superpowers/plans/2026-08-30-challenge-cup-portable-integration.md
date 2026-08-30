# Challenge Cup Portable Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the `challenge-cup-application` portability architecture into the known-good `main` workflow without changing public behavior, then produce a zero-failure, deployable challenge-cup release branch.

**Architecture:** `release/challenge-cup-portable` starts from `origin/main` and receives the remote portability history through a normal merge. Main remains authoritative for runtime behavior and contracts; portability owns scenario boundaries, compatibility adapters, externalized paths, and deployment injection. Where code moved into `scenarios/bearing`, main's latest behavior is applied to the scenario implementation and legacy modules remain thin adapters.

**Tech Stack:** Python 3.11.15, FastAPI, pytest 9.1.1, PowerShell, Docker Compose, Git LFS, Conda `moment`.

**Spec:** `docs/superpowers/specs/2026-08-30-challenge-cup-portable-integration-design.md`

## Global Constraints

- Functional baseline is `origin/main` at `c268fc0643ed83c4e1048b466ca3c80045272eba`.
- Portability source is remote branch `challenge-cup-application`, locally referenced as `origin/challenge-cup-application`, at `e3a174bf25fac8113b81dc9cf8fe03bbd557c465`.
- Never force-push, rewrite either source history, or replace the main tree with the portability tree.
- Public APIs, MQTT wire payloads, database semantics, scheduling, final decisions, and startup behavior default to main semantics.
- Scenario registry, provider injection, compatibility forwarding, external configuration, and path portability come from the portability branch when behavior remains equivalent.
- Python verification uses Conda `moment`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, an explicit 32-byte test control secret, external MOMENT paths, and a short Windows `--basetemp`.
- Production code changes follow red-green-refactor; merge bookkeeping and conflict staging are not production code.
- Do not push the integration branch or merge it into `main` without a separate user request after local verification.

---

### Task 1: Create the merge state and record the conflict inventory

**Files:**
- Modify through merge: all paths changed by `origin/challenge-cup-application`
- Verify: `docs/superpowers/specs/2026-08-30-challenge-cup-portable-integration-design.md`

**Interfaces:**
- Consumes: clean branch `release/challenge-cup-portable` containing the approved design commit.
- Produces: a merge-in-progress whose unmerged paths exactly match the Git conflict inventory; non-conflicting portability files are staged by Git.

- [ ] **Step 1: Verify immutable inputs**

Run:

```powershell
git rev-parse origin/main
git rev-parse origin/challenge-cup-application
git branch --show-current
git diff --check origin/main...origin/challenge-cup-application
```

Expected: the two revisions match Global Constraints, current branch is `release/challenge-cup-portable`, and `git diff --check` prints no errors.

- [ ] **Step 2: Merge without committing**

Run:

```powershell
git merge --no-ff --no-commit origin/challenge-cup-application
```

Expected: Git stops on content conflicts and preserves all non-conflicting changes in the index.

- [ ] **Step 3: Capture the exact unmerged paths**

Run:

```powershell
git diff --name-only --diff-filter=U
git ls-files -u
```

Expected: every unmerged file has stages 1, 2, and 3; no file outside the reported conflict set is manually modified.

### Task 2: Restore Sender and Scheduler contracts with run identity compatibility

**Files:**
- Modify: `cloud_edge_project/scenarios/bearing/ingestion/packet.py`
- Modify: `cloud_edge_project/sender_module/sender/packet.py`
- Modify: `cloud_edge_project/sender_module/sender/controller.py`
- Modify: `cloud_edge_project/scheduler/assignment_scheduler.py`
- Modify: `cloud_edge_project/scheduler/tests/test_expected_packet_count_config.py`
- Modify: `cloud_edge_project/sender_module/tests/test_target_edge_mqtt_routing.py`
- Test: `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`
- Test: `cloud_edge_project/sender_module/tests/test_run_id_propagation.py`
- Test: `cloud_edge_project/scheduler/tests/test_packet_route_v12_identity.py`

**Interfaces:**
- Consumes: main's `run_id` workflow and portability's `scenarios.bearing.ingestion.packet` entry point.
- Produces: `build_sensor_packet(..., run_id: str | None = None) -> dict[str, Any]` that omits `run_id` when absent and includes it unchanged when valid; Sender and Scheduler propagate the same value.

- [ ] **Step 1: Add the missing legacy-wire regression assertion**

Update `test_packet_builder_preserves_dictionary_and_binary_wire_bytes` so the expected dictionary contains no `run_id` when `_fixed_packet_arguments()` omits it, and add:

```python
def test_packet_builder_adds_run_id_only_when_supplied() -> None:
    from scenarios.bearing.ingestion.packet import build_sensor_packet

    packet = build_sensor_packet(**_fixed_packet_arguments(), run_id="run_demo_001")

    assert packet["run_id"] == "run_demo_001"
```

- [ ] **Step 2: Run the compatibility tests and verify RED**

Run from `cloud_edge_project`:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q tests/scenarios/bearing/test_ingestion_layout_compatibility.py sender_module/tests/test_run_id_propagation.py
```

Expected: the legacy-wire test fails because the current portability implementation emits `run_id: None`; the supplied-run test passes or exposes missing propagation separately.

- [ ] **Step 3: Implement optional-field serialization**

Build the packet without a `run_id` key, validate a provided value, then add it conditionally:

```python
packet = {
    "device_id": device_id,
    "task_id": task_id,
    "bearing_id": bearing_id,
    "packet_id": f"{task_id}_{bearing_id}_pkt_{sequence_number:03d}",
    "sender_id": sender_id,
    "sequence_number": sequence_number,
    "end_generate_timestamp_ns": end_generate_timestamp_ns,
    "data": data,
}
if run_id is not None:
    packet["run_id"] = run_id
return packet
```

Resolve Sender/Scheduler conflicts by retaining main's run grouping and routing decisions while importing the packet builder through the portability compatibility layer.

- [ ] **Step 4: Run Sender and Scheduler GREEN tests**

Run:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q tests/scenarios/bearing/test_ingestion_layout_compatibility.py sender_module/tests scheduler/tests
```

Expected: all selected tests pass, including old packets without `run_id` and new packets with `run_id`.

- [ ] **Step 5: Stage this conflict group**

Run `git add` with only the files listed in this task, then confirm none remain in `git diff --name-only --diff-filter=U`.

### Task 3: Resolve Cloud and Edge assembly without changing public routes

**Files:**
- Modify: `cloud_edge_project/cloud_service/app.py`
- Modify: `cloud_edge_project/cloud_service/device_arbitration/summary_contract.py`
- Modify: `cloud_edge_project/cloud_service/global_analysis/periodic.py`
- Modify: `cloud_edge_project/cloud_service/model_update/service.py`
- Modify: `cloud_edge_project/edge_service/src/edge_runtime/coordinator.py`
- Test: `cloud_edge_project/cloud_service/tests/test_scenario_registry_compatibility.py`
- Test: `cloud_edge_project/cloud_service/tests/test_stage6_arbitration_equivalence.py`
- Test: `cloud_edge_project/cloud_service/tests/test_stage7_storage_registration.py`
- Test: `cloud_edge_project/edge_service/verification/test_v12_decision_flow.py`

**Interfaces:**
- Consumes: portability scenario registry/providers and main Cloud model-update, periodic-analysis, and Edge coordination behavior.
- Produces: unchanged FastAPI routes and response schemas whose scenario-specific work is injected through providers.

- [ ] **Step 1: Run the Cloud/Edge tests against the unresolved merge inputs**

Inspect stages with `git show :2:<path>` and `git show :3:<path>`, then run the listed tests after creating syntactically resolved candidate files. The first run must demonstrate at least one expected conflict-induced failure before production behavior is changed.

- [ ] **Step 2: Resolve application assembly**

Keep all main route declarations and lifecycle operations in `cloud_service/app.py`. Register bearing providers through `bootstrap.scenarios.load_scenarios()` and obtain scenario capabilities from the registry instead of importing bearing implementation modules into platform services.

- [ ] **Step 3: Resolve Cloud services and Edge coordinator**

Keep main's periodic synchronization, model-update state transitions, and coordinator run isolation. Replace only direct bearing implementation imports with exports from `compatibility.bearing_v12` or registry-provided callables. Do not duplicate repository writes or MQTT publication.

- [ ] **Step 4: Run Cloud/Edge GREEN tests**

Run:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q cloud_service/tests edge_service/verification tests/contracts tests/architecture
```

Expected: all selected tests pass except an explicitly recorded main-baseline failure unrelated to these files; no new route, lifecycle, or architecture failure remains.

- [ ] **Step 5: Stage this conflict group**

Stage only the five production files and their changed tests, then confirm these paths disappear from the unmerged list.

### Task 4: Preserve main Summary semantics behind the bearing scenario boundary

**Files:**
- Modify: `cloud_edge_project/summary_service/action_scorer.py`
- Modify: `cloud_edge_project/summary_service/aggregation.py`
- Modify: `cloud_edge_project/summary_service/contracts.py`
- Modify: `cloud_edge_project/summary_service/repository.py`
- Modify: `cloud_edge_project/summary_service/runtime.py`
- Modify: `cloud_edge_project/summary_service/service.py`
- Modify: `cloud_edge_project/summary_service/suggestion_llm.py`
- Modify: `cloud_edge_project/summary_service/suggestions.py`
- Modify: `cloud_edge_project/scenarios/bearing/summary_service/*.py`
- Test: `cloud_edge_project/scenarios/bearing/summary_service/tests/*.py`
- Test: `cloud_edge_project/cloud_service/tests/test_summary_device_arbitration_contract.py`
- Test: `cloud_edge_project/cloud_service/tests/test_summary_window_sync.py`

**Interfaces:**
- Consumes: main's final-decision, storage ports/schema, window synchronization, metrics, and suggestion behavior.
- Produces: canonical implementation in `scenarios.bearing.summary_service`; `summary_service.*` remains an import/startup compatibility surface with identical API and persisted semantics.

- [ ] **Step 1: Add equivalence coverage before resolving implementations**

Add or retain assertions that legacy and scenario imports expose the same callable/class objects for action scoring, aggregation, contracts, repository, runtime, service, and suggestions. Add an end-to-end repository assertion that a main-format final decision can be written through `summary_service` and read through `scenarios.bearing.summary_service` with the same fields.

- [ ] **Step 2: Run Summary tests and verify RED**

Run:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q summary_service/tests scenarios/bearing/summary_service/tests cloud_service/tests/test_summary_device_arbitration_contract.py cloud_service/tests/test_summary_window_sync.py
```

Expected: equivalence or storage tests fail until main's newer storage and final-decision semantics are present behind the scenario boundary.

- [ ] **Step 3: Establish one canonical implementation**

Move or port main's current logic into `scenarios/bearing/summary_service`. Replace each legacy module body with explicit imports/re-exports from the matching scenario module while preserving `summary_service.app:app` as the startup target. Keep main's storage modules and `sync_contract` reachable through the canonical scenario runtime instead of recreating tables or metrics.

- [ ] **Step 4: Run Summary GREEN tests**

Run the command from Step 2 plus:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q tests/contracts/test_arbitration_engine.py tests/contracts/test_consistency_engine.py tests/scenarios/bearing/test_decision_policy.py
```

Expected: all Summary, arbitration, storage, and decision tests pass with no duplicate-schema or import-identity failures.

- [ ] **Step 5: Stage this conflict group**

Stage the eight legacy conflicts, canonical scenario files, and tests; verify no Summary path remains unmerged.

### Task 5: Reconcile startup portability and repair pre-existing baseline fixtures

**Files:**
- Modify: `start_project.ps1`
- Modify: `cloud_edge_project/edge_service/status_reporter_tests/test_multi_node_config.py`
- Create: `cloud_edge_project/sender_module/config/local.two-senders.json`
- Test: `cloud_edge_project/frontend/verification/test_start_project_host_process_detection.py`
- Test: `cloud_edge_project/edge_service/verification/test_start_project_moment_paths.py`
- Test: `cloud_edge_project/internet_service/network_simulator/verification/test_project_links.py`

**Interfaces:**
- Consumes: main's official four-stage startup and portability's external path/preflight rules.
- Produces: read-only `-CheckConfig`, explicit assets/secrets, safe process ownership, and a tracked two-Sender demonstration fixture.

- [ ] **Step 1: Lock intended defaults in tests**

Update the Edge default test to assert the same network-proxy Scheduler URL used by the official main startup configuration. Add a Sender config test that loads the tracked two-Sender fixture and asserts exactly `sender_01` and `sender_02` with distinct data roots and MQTT routes.

- [ ] **Step 2: Run baseline tests and verify the two known failures**

Run:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q edge_service/status_reporter_tests/test_multi_node_config.py sender_module/tests/test_config.py
```

Expected before fixture/default alignment: the Scheduler URL assertion and missing two-Sender file fail for the same reasons recorded in the spec.

- [ ] **Step 3: Add the deterministic two-Sender fixture and resolve startup script**

Create `local.two-senders.json` using the schema consumed by `sender.config.load_config`, with two sender entries, the repository-relative demo data directory, Scheduler proxy endpoints, and MQTT broker values matching `start_project.ps1`. Resolve `start_project.ps1` by retaining main's stages and cleanup behavior, then merge portability's `Resolve-DeploymentPath`, external `CLOUD_MOMENT_*`, image revision check, read-only preflight, and safe process ownership checks.

- [ ] **Step 4: Run startup and baseline GREEN tests**

Run:

```powershell
conda run -n moment python -m pytest -p no:cacheprovider -q edge_service/status_reporter_tests/test_multi_node_config.py sender_module/tests/test_config.py frontend/verification edge_service/verification/test_start_project_moment_paths.py internet_service/network_simulator/verification/test_project_links.py
```

Expected: both pre-existing main failures and all startup portability tests pass.

- [ ] **Step 5: Stage the final conflict and fixture group**

Stage `start_project.ps1`, the aligned test, and the tracked fixture; verify `git diff --name-only --diff-filter=U` is empty.

### Task 6: Complete the merge and run the full automated gate

**Files:**
- Verify: all merged files
- Modify only if a failing regression test identifies a concrete defect in the merged behavior.

**Interfaces:**
- Consumes: all resolved and staged conflict groups.
- Produces: a merge commit with zero unmerged paths and zero automated test failures.

- [ ] **Step 1: Check merge hygiene**

Run:

```powershell
git diff --name-only --diff-filter=U
rg -n "^(<<<<<<<|=======|>>>>>>>)" --glob '!*.md'
git diff --check --cached
```

Expected: no unmerged paths, no conflict markers, and no whitespace errors.

- [ ] **Step 2: Run the complete test suite**

Set the explicit test secret and existing external MOMENT asset paths, then run from `cloud_edge_project`:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
conda run -n moment python -m pytest -p no:cacheprovider -q --basetemp "$env:TEMP\challenge-portable-final"
```

Expected: `0 failed, 0 errors`.

- [ ] **Step 3: Run portable scenario smoke checks**

Run:

```powershell
conda run -n moment python scripts/demo_virtual_power_plant.py
conda run -n moment python internet_service/network_simulator/verification/minimal_markov_validation.py
```

Expected: VPP reports `"result": "PASS"`; Markov validation loads all configured links and writes its result without an exception.

- [ ] **Step 4: Commit the merge**

Run:

```powershell
git commit -m "merge: integrate portable challenge cup architecture"
```

Expected: a two-parent merge commit whose first-parent history retains main and whose second parent is the audited portability revision.

### Task 7: Deployment preflight and local handoff

**Files:**
- Verify: `.env.example`
- Verify: `README.md`
- Verify: `start_project.ps1`

**Interfaces:**
- Consumes: completed merge commit, external MOMENT assets, Conda `moment`, test-only control secret, and local Docker availability.
- Produces: evidence for configuration readiness and a precise list of any host-only blockers; it does not push or merge remote branches.

- [ ] **Step 1: Run arbitrary-CWD Edge import validation**

From a temporary directory, run `edge_service/run_edge_service.py --check-import` with the explicit control secret. Expected: exit code 0 without importing from the current working directory.

- [ ] **Step 2: Run read-only project preflight**

Run:

```powershell
.\start_project.ps1 -CheckConfig -SkipLLM
```

Expected: either a complete pass or a precise external blocker such as Docker Desktop not running; the command must not create a secret, stop a process, or start a container.

- [ ] **Step 3: Inspect final history and working tree**

Run:

```powershell
git log -1 --format=fuller
git show --summary --pretty=raw HEAD
git status --short --branch
```

Expected: branch `release/challenge-cup-portable`, merge commit with two parents, and no tracked modifications. Generated test artifacts may remain only in ignored paths.

- [ ] **Step 4: Report without remote mutation**

Report the merge commit, exact test counts, smoke/preflight results, unresolved host-only blockers, and the local worktree path. Do not push, tag, open a PR, or merge to `main` without new authorization.
