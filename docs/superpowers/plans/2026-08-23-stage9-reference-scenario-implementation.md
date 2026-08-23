# Stage 9 Reference Scenario Implementation Plan

## Objective

Add a test-only `reference_inspection` plugin that exercises the existing
scenario registry, generic diagnosis contracts, consistency engine,
arbitration engine, and storage-provider entry without modifying any
production or bearing source file.

## Guardrails

- Keep the fixture under `cloud_edge_project/tests/fixtures/scenarios/`.
- Do not register it in production `bootstrap/scenarios.py`.
- Implement exactly six capabilities: input, edge inference, cloud diagnosis,
  consistency policy, arbitration policy, and storage provider.
- Do not add model update, global analysis, training, API, MQTT, environment
  configuration, or production startup behavior.
- Use deterministic fixture values and generic identifiers only.
- Do not import `scenarios.bearing` or `compatibility.bearing_v12` from the
  fixture package.
- Do not modify generic engines merely to make the fixture fit.
- Do not touch the user's existing deleted or untracked files.
- Complete and report every task before starting the next one.

## Task 1: Freeze the Stage 9 baseline

Actions:

1. Record branch, HEAD, and protected working-tree entries.
2. Run registry, generic contract, consistency, arbitration, storage, and
   architecture tests with the established Python environment.
3. Confirm the full-suite baseline is 760 passing tests at Stage 8 HEAD.
4. Confirm production code contains no `reference_inspection` vocabulary.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/contracts/test_scenario_registry.py `
  cloud_edge_project/tests/contracts/test_consistency_engine.py `
  cloud_edge_project/tests/contracts/test_arbitration_engine.py `
  cloud_edge_project/tests/contracts/test_storage_provider_contract.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

No file changes are allowed in this task.

## Task 2: Add the reference plugin and registry contracts

Files:

- add `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/__init__.py`;
- add `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/plugin.py`;
- add `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/providers.py`;
- add `cloud_edge_project/tests/contracts/test_reference_inspection_plugin.py`.

Actions:

1. Declare `REFERENCE_INSPECTION_CAPABILITIES` containing exactly the six
   approved capability constants.
2. Declare a `ScenarioManifest` with `scenario_id="reference_inspection"` and
   a fixed test version.
3. Build immutable, resolved `CapabilityBinding` entries for every declared
   capability.
4. Validate exact manifest/binding equality without reading environment
   configuration.
5. Register the plugin in a fresh test-owned `ScenarioRegistry`.
6. Verify all six providers resolve and omitted optional capabilities raise
   the existing `MissingScenarioCapabilityError`.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/contracts/test_reference_inspection_plugin.py `
  cloud_edge_project/tests/contracts/test_scenario_registry.py -q
```

## Task 3: Implement deterministic input, edge, and cloud providers

Files:

- modify `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/providers.py`;
- add `cloud_edge_project/tests/regression/test_reference_inspection_flow.py`.

Actions:

1. Add a fixed adapter that emits one immutable `ScenarioInferenceRequest`.
2. Use only generic request fields and evidence such as a numeric defect score.
3. Implement the full existing `EdgeInferenceProvider` surface with a minimal
   deterministic test runtime; do not add a model provider capability.
4. Implement `CloudDiagnosisProvider` and its handler with deterministic
   generic diagnosis output.
5. Use fixed, non-bearing model IDs and versions solely as fixture metadata.
6. Reject wrong-scenario and incomplete inputs explicitly.
7. Verify edge and cloud outputs contain the exact expected generic conclusion
   and no bearing identifiers or labels.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/regression/test_reference_inspection_flow.py `
  cloud_edge_project/tests/contracts/test_reference_inspection_plugin.py -q
```

## Task 4: Exercise generic consistency and arbitration

Files:

- modify `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/providers.py`;
- modify `cloud_edge_project/tests/regression/test_reference_inspection_flow.py`.

Actions:

1. Implement a deterministic `ConsistencyPolicy` using only
   `ConsistencyRequest` fields.
2. Produce a stable decision for matching results and a stable conflict for
   different inspection states.
3. Implement the existing arbitration-policy surface with one safety-first
   rule for a high-risk detected defect.
4. Run the unchanged `ConsistencyEngine` and `ArbitrationEngine` with providers
   resolved from the registry.
5. Assert state, confidence, action, conflict, degradation, dominant unit,
   rule ID, and final `stop_and_inspect` action.
6. Prove no bearing policy or compatibility mapper is imported or called.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/regression/test_reference_inspection_flow.py `
  cloud_edge_project/tests/contracts/test_consistency_engine.py `
  cloud_edge_project/tests/contracts/test_arbitration_engine.py -q
```

## Task 5: Add and verify the reference storage provider

Files:

- modify `cloud_edge_project/tests/fixtures/scenarios/reference_inspection/providers.py`;
- modify `cloud_edge_project/tests/regression/test_reference_inspection_flow.py`.

Actions:

1. Implement `StorageProvider.initialize()` with an idempotent fixture-only
   schema registration statement.
2. Keep fixture record behavior in memory; do not add a production repository.
3. Resolve the provider through `ScenarioRegistry`.
4. Pass it to the unchanged generic database initializer alongside the current
   production storage provider, using only a temporary test database.
5. Initialize twice, write and read one reference record, and prove it survives
   repeated initialization without duplication or schema damage.
6. Confirm existing production schema and migrations have no diff.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/regression/test_reference_inspection_flow.py `
  cloud_edge_project/tests/contracts/test_storage_provider_contract.py -q
```

## Task 6: Add portability architecture guards

Files:

- add `cloud_edge_project/tests/architecture/test_reference_inspection_portability.py`.

Actions:

1. Parse the fixture imports and reject direct bearing scenario or bearing
   compatibility imports.
2. Scan production `bootstrap`, `core`, `scheduler`, `edge_service`, and
   `cloud_service` sources for `reference_inspection` imports or literals.
3. Assert production registry builders still return only the bearing scenario.
4. Assert the fixture uses no bearing vocabulary from the migration plan.
5. Assert the Stage 9 implementation file set remains confined to fixture and
   test paths.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/architecture/test_reference_inspection_portability.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

## Task 7: Run focused and full regression

Actions:

1. Run every Stage 9 contract, regression, and architecture test.
2. Run existing registry, consistency, arbitration, storage, edge, cloud,
   scheduler, compatibility, and model-lifecycle tests.
3. Run `git diff --check` and inspect the complete Stage 9 source diff.
4. Confirm no production or bearing source file changed.
5. Run the complete repository suite with the formal model paths.
6. Commit only Stage 9 fixture and test files.

Acceptance:

- the full suite has at least 760 baseline tests plus new Stage 9 tests;
- no existing output or public behavior changes;
- only the protected user-owned changes remain outside Stage 9 commits.

## Task 8: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved Stage 9 design and this
   implementation plan.
2. Fix every Critical and Important issue.
3. Rerun affected tests and the complete suite after any fix.
4. Commit review fixes separately when needed.
5. Confirm the final diff contains only Stage 9 fixture, test, and documentation
   files.
6. Report all required stage-completion details and do not enter Stage 10
   automatically.

Acceptance:

- reviewer reports no remaining Critical or Important issue;
- all focused and full tests pass;
- production and bearing source remain unchanged;
- the user-owned worktree protection list is unchanged.

## Formal Test Environment

Use the established Stage 8 environment for model-dependent and full tests:

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
