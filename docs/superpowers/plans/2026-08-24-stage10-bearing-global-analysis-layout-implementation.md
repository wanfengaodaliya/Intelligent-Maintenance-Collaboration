# Stage 10 Bearing Global Analysis Physical Layout Implementation Plan

## Objective

Move the remaining bearing-specific global-analysis data source, configuration,
candidate rules, and result assembly from `cloud_service.global_analysis` into
`scenarios.bearing.cloud.global_analysis`. Keep platform orchestration,
generic analyzers, persistence, periodic execution, APIs, and all observable
V1.2 behavior unchanged through explicit Provider injection and bearing V1.2
compatibility exports.

## Success Criteria

- Bearing V1.2 history loading, revision selection, review pairing, thresholds,
  candidate rules, and scenario result assembly each have one implementation
  owner under `scenarios.bearing.cloud.global_analysis`.
- `cloud_service.global_analysis` contains only generic orchestration,
  analyzers, persistence, runtime contracts, and explicit legacy shims.
- The application and periodic task obtain the complete global-analysis
  runtime through the scenario registry; neither imports `scenarios.bearing`.
- Existing `cloud_service.global_analysis.contracts`,
  `cloud_service.global_analysis.v12_data_source`, direct
  `GlobalAnalysisService(database_path)` construction, and existing Provider
  analyzer exports remain compatible.
- API paths, response fields, candidate ordering, thresholds, SQL behavior,
  revision ordering, persistence, logging, and per-subject failure isolation
  remain equivalent.
- The full suite passes with at least the accepted 881-test baseline plus new
  contracts.
- Independent review reports no remaining Critical or Important issue.

## Guardrails

- Complete and report each task before starting the next task.
- Use tests before each production move and require failures to identify only
  the intended missing layout or injection boundary.
- Do not change formulas, threshold values, result names, candidate ordering,
  SQL, table names, revision ranking, JSON tolerance, APIs, periodic frequency,
  or exception handling.
- Keep generic device, packet, arbitration, physical-evidence, repository, and
  scheduling behavior in the platform package.
- Keep `build_analyzers()` as a compatibility surface while adding the minimal
  complete runtime assembly used by new production wiring.
- Do not move or modify offline training, model update, storage layout, H5,
  MOMENT, model assets, Git LFS, scheduler, consistency, or arbitration rules.
- Do not use wildcard imports or leave duplicate production implementations.
- Do not touch protected user-owned deleted or untracked files.
- Stop if compatibility requires changing externally visible analysis behavior
  or if a broader subsystem refactor becomes necessary.

## Task 1: Freeze the global-analysis baseline

Actions:

1. Record branch, HEAD, complete short worktree status, and protected entries.
2. Record SHA-256 hashes for all tracked Python files under:
   - `cloud_service/global_analysis`;
   - `scenarios/bearing/cloud/global_analysis`;
   - the global-analysis portions of `cloud_service/app.py`,
     `core/scenario_plugin.py`, and `scenarios/bearing/plugin.py`.
3. Record the import graph and public names for contracts, V1.2 data source,
   service, periodic executor, problem detector, and bearing Provider.
4. Freeze normalized golden results for:
   - V1.2 current-revision selection;
   - bearing review pairing;
   - round-closure and revision-deduplication summaries;
   - device, packet, arbitration, and physical-evidence analysis;
   - bearing risk, cloud review, and maintenance recommendations;
   - problem candidates and their ordering;
   - direct service construction, API execution, and periodic execution.
5. Treat generated IDs and timestamps as formatted nondeterministic fields;
   compare every other output exactly.
6. Run all existing global-analysis, Provider, registry, API, architecture,
   storage-neighbor, and model-update-neighbor tests.
7. Run the complete suite and confirm the accepted 881-test baseline.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/cloud_service/tests/test_global_analysis_analyzers.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_service.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12_e2e.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_periodic.py `
  cloud_edge_project/cloud_service/tests/test_portability.py `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_capability_providers.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

No file changes are allowed in this task.

## Task 2: Add failing layout and behavior contracts

Files:

- add
  `cloud_edge_project/tests/scenarios/bearing/test_global_analysis_layout_compatibility.py`;
- modify
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Define retained public exports for the old contracts and V1.2 data-source
   modules.
2. Add old/compatibility/scenario object-identity contracts.
3. Add thin-shim AST contracts for legacy files and single-owner contracts for
   protected configuration, data-source, and candidate-rule definitions.
4. Add source rules prohibiting application and generic global-analysis files
   from importing `scenarios.bearing`.
5. Add contracts requiring the formal API and periodic paths to receive a
   Provider-built runtime rather than use an implicit bearing default.
6. Add independent goldens for configuration values, V1.2 loading,
   scenario-result fields, candidate contents/order, direct construction, and
   exception propagation.
7. Run the new tests and record that failures are limited to missing target
   modules, missing runtime assembly, legacy implementation ownership, or the
   old hard-coded wiring.

Acceptance:

- all pre-existing global-analysis tests remain green;
- new failures describe only the intended migration boundary;
- no production file changes occur.

Commit boundary:

```text
test: define global analysis layout contracts
```

## Task 3: Separate the generic runtime contract and move bearing configuration

Files:

- add `cloud_edge_project/cloud_service/global_analysis/runtime_contracts.py`;
- modify generic analyzers that currently type against the mixed legacy
  configuration;
- move the existing bearing configuration implementation to
  `cloud_edge_project/scenarios/bearing/cloud/global_analysis/config.py`;
- add
  `cloud_edge_project/compatibility/bearing_v12/global_analysis_exports.py`;
- replace `cloud_edge_project/cloud_service/global_analysis/contracts.py` with
  an explicit compatibility shim;
- extend focused layout tests.

Actions:

1. Define the smallest structural runtime configuration protocol containing
   only fields used by platform analyzers.
2. Keep the generic default task limit in the platform runtime contracts.
3. Move the legacy `GlobalAnalysisConfig`, its exact defaults, and all bearing
   thresholds to the bearing scenario configuration module.
4. Repoint platform analyzer type imports to the generic protocol without
   changing runtime access or calculations.
5. Explicitly re-export legacy configuration names through compatibility and
   the old contracts path.
6. Assert old/new object identity, default values, frozen behavior, and exact
   constructor compatibility.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_global_analysis_layout_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_analyzers.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_service.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing global analysis configuration
```

## Task 4: Move the bearing V1.2 data source

Files:

- add
  `cloud_edge_project/scenarios/bearing/cloud/global_analysis/v12_data_source.py`;
- extend
  `cloud_edge_project/compatibility/bearing_v12/global_analysis_exports.py`;
- replace `cloud_edge_project/cloud_service/global_analysis/v12_data_source.py`
  with an explicit compatibility shim;
- extend focused layout tests.

Actions:

1. Move `V12GlobalAnalysisDataSource` and its private SQL/JSON/revision helpers
   without logic edits.
2. Preserve every query, sort key, identity key, availability flag, output
   field, and malformed-JSON behavior.
3. Export the class explicitly through compatibility and the old module.
4. Assert old/new class identity and cold-import behavior.
5. Compare complete normalized outputs for current revisions, late revisions,
   review pairs, evidence, packet summaries, arbitration state, absent tables,
   and malformed stored payloads.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_global_analysis_layout_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12_e2e.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing global analysis data source
```

## Task 5: Move bearing candidate rules and generalize scenario result assembly

Files:

- add
  `cloud_edge_project/scenarios/bearing/cloud/global_analysis/problem_detector.py`;
- modify `cloud_edge_project/cloud_service/global_analysis/problem_detector.py`;
- modify `cloud_edge_project/cloud_service/global_analysis/service.py`;
- modify
  `cloud_edge_project/scenarios/bearing/cloud/global_analysis/provider.py`;
- extend `core/scenario_plugin.py` with the approved minimal runtime assembly
  contract;
- extend focused and architecture tests.

Actions:

1. Keep packet and device-arbitration candidates in the generic detector.
2. Move only the cloud-bearing-review candidate rule to the bearing detector,
   preserving its threshold, evidence, severity, action, IDs, persistence, and
   position in the final candidate list.
3. Add a Provider-built runtime containing the data source, configuration,
   scenario analysis step, and scenario candidate step.
4. Make the generic service merge mappings returned by the scenario analysis
   step instead of naming bearing analyzers or result fields.
5. Run generic candidates and scenario candidates in an explicit order that
   reproduces the baseline list exactly.
6. Keep `build_analyzers()` unchanged as the legacy Stage 3 Provider surface.
7. Preserve direct `GlobalAnalysisService(database_path)` behavior through a
   lazy bearing V1.2 compatibility assembly; do not use that path from formal
   application or periodic wiring.
8. Assert exact normalized result, candidate ordering, exception propagation,
   and direct-construction compatibility.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_global_analysis_layout_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_analyzers.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_service.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12.py `
  cloud_edge_project/cloud_service/tests/test_portability.py `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_capability_providers.py -q
git diff --check
```

Commit boundary:

```text
refactor: isolate bearing global analysis extensions
```

## Task 6: Switch API and periodic execution to Provider runtime injection

Files:

- modify `cloud_edge_project/cloud_service/app.py`;
- modify `cloud_edge_project/cloud_service/global_analysis/periodic.py`;
- modify scenario Provider and compatibility exports only where required by
  the finalized injection contract;
- finalize focused and architecture tests.

Actions:

1. Have the API resolve the requested scenario through the existing registry
   and pass its complete runtime to `GlobalAnalysisService`.
2. Have `_run_periodic_global_analysis_once` resolve the same Provider and pass
   a runtime factory to `run_all`.
3. Keep subject discovery, default compatibility scenario, task limit,
   execution order, persistence, error conversion, and per-subject failure
   isolation unchanged.
4. Verify unsupported scenarios and missing capabilities produce the existing
   errors without implicit bearing fallback in generic code.
5. Verify application and periodic modules do not import the bearing scenario.
6. Verify old API requests, latest-result queries, and direct periodic calls
   retain their existing outputs.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_global_analysis_layout_compatibility.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_periodic.py `
  cloud_edge_project/cloud_service/tests/test_global_analysis_v12_e2e.py `
  cloud_edge_project/cloud_service/tests/test_scenario_registry_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_portability.py -q
git diff --check
```

Commit boundary:

```text
refactor: inject scenario global analysis runtime
```

## Task 7: Run focused and full acceptance

Actions:

1. Run all new layout, identity, thin-shim, single-owner, cold-import,
   injection, and architecture tests.
2. Run all existing global-analysis analyzer, service, V1.2, periodic, API,
   Provider, registry, storage-neighbor, and model-update-neighbor tests.
3. Compare normalized golden outputs with Task 1, excluding only documented
   random IDs and timestamps.
4. Compare all frozen source and asset hashes; only approved source,
   compatibility, tests, and documents may differ.
5. Run the complete repository suite and require at least the accepted 881
   baseline tests plus new tests.
6. Run `git diff --check` and inspect every file in the batch commit range.
7. Confirm protected user worktree entries remain unchanged and uncommitted.

Acceptance:

- all focused and full tests pass;
- analysis values, thresholds, fields, candidates, SQL behavior, persistence,
  APIs, errors, and periodic behavior are equivalent;
- only this global-analysis batch's source, compatibility, tests, and documents
  are committed.

No commit is required unless acceptance exposes a test-only gap.

## Task 8: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved design, this plan, and the
   complete global-analysis batch commit range.
2. Require explicit review of responsibility ownership, compatibility shims,
   config defaults, SQL fidelity, revision ranking, candidate ordering,
   Provider injection, direct construction, import direction, API/periodic
   behavior, and commit confinement.
3. Fix every Critical and Important finding with surgical changes.
4. Rerun affected focused tests and the full suite after every fix.
5. Commit review fixes separately when needed.
6. Report final commits, test counts, golden/hash results, review verdict,
   protected worktree status, and remaining Stage 10 batches.
7. Stop after this global-analysis batch; do not automatically start offline
   training/model-update or storage physical movement.

Acceptance:

- reviewer reports no remaining Critical or Important issue;
- all tests and architecture guards pass;
- the global-analysis batch is independently releasable.

## Formal Test Environment

Use:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
PYTHONPATH=D:\codex\edge_status_reporter\cloud_edge_project;D:\codex\edge_status_reporter\cloud_edge_project\sender_module
EDGE_CONTROL_SHARED_SECRET=stage0-stage0-stage0-stage0-secret
CLOUD_MOMENT_DEPLOYMENT_DIR=D:\codex\edge_status_reporter\local_experiment\deploy\light_adapt
CLOUD_MOMENT_CHECKPOINT_PATH=D:\codex\edge_status_reporter\local_experiment\analysis\final_model\moment_final_chance\SCL05\fold_3\best_model.pt
CLOUD_MOMENT_CONDITION_NORM_PATH=D:\codex\edge_status_reporter\local_experiment\analysis\final_model\moment_final_chance\SCL05\fold_3\condition_norm.json
CLOUD_MOMENT_PRETRAINED_PATH=D:\codex\edge_status_reporter\experiments\diagnosis_models\moment\pretrained\MOMENT-1-small
```

Python executable:

```text
D:\codex\edge_status_reporter\.cache\stage0-py312\Scripts\python.exe
```

