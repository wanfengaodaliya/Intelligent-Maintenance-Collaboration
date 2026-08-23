# Stage 10 Bearing Ingestion Physical Layout Implementation Plan

## Objective

Complete only the first independently accepted Stage 10 batch: move bearing
MAT reading, packet construction, and source mapping from `sender.*` into
`scenarios.bearing.ingestion`, while preserving the old sender imports through
`compatibility.bearing_v12` and changing no observable behavior.

## Success Criteria

- The three business implementations have one owner under
  `scenarios.bearing.ingestion`.
- `BearingInputAdapter` imports the scenario-local implementations.
- Existing `sender.mat_reader`, `sender.packet`, and `sender.source_mapping`
  imports remain valid and resolve to the same public objects.
- MAT records/windows, packet dictionaries/bytes/errors, and source-mapping
  database behavior remain exactly equivalent.
- Sender shims do not directly import `scenarios.bearing` and contain no
  business implementation.
- The full suite passes with at least the 781-test Stage 9 baseline plus the
  new Stage 10 tests.
- An independent review reports no remaining Critical or Important finding.

## Guardrails

- Perform tasks in order and report completion after every task.
- Do not start the next physical-layout batch in this plan.
- Do not change controller, MQTT, scheduler, retry, acknowledgement, timing,
  H5, MOMENT, storage-schema ownership, training, or global-analysis behavior.
- Do not alter expected values to make equivalence tests pass.
- Do not use wildcard imports or duplicate implementation.
- Keep each legacy sender module as an explicit compatibility shim.
- Do not touch the protected user-owned deleted or untracked files.
- Stop if a target file overlaps a new user edit or a broader production logic
  change becomes necessary.

## Task 1: Freeze the ingestion baseline

Actions:

1. Record the branch, HEAD, and complete short worktree status.
2. Confirm the protected user-owned entries are unchanged.
3. Inspect the current public names and import graph for all three modules.
4. Run the existing sender source-mapping, target-routing, bearing input
   adapter, compatibility, and dependency-rule tests.
5. Run the complete suite with the formal test environment and confirm the
   Stage 9 baseline remains 781 passing tests.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/sender_module/tests/test_packet_source_mapping.py `
  cloud_edge_project/sender_module/tests/test_target_edge_mqtt_routing.py `
  cloud_edge_project/tests/scenarios/bearing/test_input_adapter.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

No file changes are allowed in this task.

## Task 2: Add failing physical-layout compatibility contracts

Files:

- add
  `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`;
- modify
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Add import contracts for the planned scenario-local modules and the V1.2
   compatibility export module.
2. Assert old and new public exceptions, data classes, functions, and relevant
   constants have the approved identity/value relationship.
3. Add source-tree rules requiring legacy sender modules to import only the
   compatibility boundary, not `scenarios.bearing`.
4. Add rules requiring the scenario provider to avoid the three sender
   implementation paths.
5. Add single-owner rules for the business class/function definitions.
6. Run only these tests and record that they fail for the expected missing
   layout, before modifying production files.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

Acceptance:

- failures point only to the not-yet-created target modules or old dependency
  direction;
- no existing behavior assertion regresses.

## Task 3: Move MAT reading behind the compatibility chain

Files:

- add `cloud_edge_project/scenarios/bearing/ingestion/mat_reader.py`;
- add `cloud_edge_project/compatibility/bearing_v12/ingestion_exports.py`;
- replace `cloud_edge_project/sender_module/sender/mat_reader.py` with an
  explicit thin shim;
- extend
  `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`.

Actions:

1. Move the complete current MAT implementation without logic edits.
2. Export every retained public MAT name explicitly from
   `compatibility.bearing_v12.ingestion_exports`.
3. Re-export those names explicitly from `sender.mat_reader` and define
   `__all__`.
4. Assert old/new class, exception, function, and constant compatibility.
5. Exercise fixed valid and invalid MAT inputs through both import paths.
6. Compare record fields, arrays, rates, windows, indexes, values, units,
   temperatures, and error type/message exactly.
7. Run the existing bearing adapter tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py `
  cloud_edge_project/tests/scenarios/bearing/test_input_adapter.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing mat ingestion implementation
```

## Task 4: Move packet construction behind the compatibility chain

Files:

- add `cloud_edge_project/scenarios/bearing/ingestion/packet.py`;
- extend `cloud_edge_project/compatibility/bearing_v12/ingestion_exports.py`;
- replace `cloud_edge_project/sender_module/sender/packet.py` with an explicit
  thin shim;
- extend
  `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`.

Actions:

1. Move the complete current packet implementation without logic edits.
2. Add explicit compatibility exports and a matching shim `__all__`.
3. Assert old/new exception and public function identity and constant values.
4. Build identical packets through both paths and compare the complete
   dictionaries, IDs, field ordering semantics, and UTF-8 serialized bytes.
5. Compare every covered invalid-input exception type and message exactly.
6. Run sender routing and bearing adapter tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py `
  cloud_edge_project/tests/scenarios/bearing/test_input_adapter.py `
  cloud_edge_project/sender_module/tests/test_target_edge_mqtt_routing.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing packet implementation
```

## Task 5: Move source mapping behind the compatibility chain

Files:

- add `cloud_edge_project/scenarios/bearing/ingestion/source_mapping.py`;
- extend `cloud_edge_project/compatibility/bearing_v12/ingestion_exports.py`;
- replace `cloud_edge_project/sender_module/sender/source_mapping.py` with an
  explicit thin shim;
- extend
  `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`.

Actions:

1. Move the complete current source-mapping implementation without logic
   edits, including unchanged SQL and dataset metadata.
2. Add explicit compatibility exports and a matching shim `__all__`.
3. Assert old/new store class and filename-parser function identity.
4. Use temporary databases to compare initialization, historical-row reads,
   inserts, upserts, repeated construction, normalized source values, and
   result shapes through old and new paths.
5. Run the complete existing source-mapping suite before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py `
  cloud_edge_project/sender_module/tests/test_packet_source_mapping.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing source mapping implementation
```

## Task 6: Cut the bearing provider over to local implementations

Files:

- modify `cloud_edge_project/scenarios/bearing/ingestion/provider.py`;
- modify `cloud_edge_project/scenarios/bearing/ingestion/__init__.py` only if
  an already-required explicit scenario export needs to be exposed;
- finalize
  `cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py`;
- finalize
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Replace the three `sender.*` implementation imports in the provider with
   scenario-local imports.
2. Run the complete prepare, window, packet, persistence, and serialization
   path through the registered `BearingInputAdapter`.
3. Verify old sender paths still resolve through
   `compatibility.bearing_v12.ingestion_exports`.
4. Verify sender shims contain no implementation definitions, SQL, bearing
   signal maps, or direct scenario imports.
5. Verify no dependency cycle is introduced and business definitions have a
   single owner.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_ingestion_layout_compatibility.py `
  cloud_edge_project/tests/scenarios/bearing/test_input_adapter.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/sender_module/tests/test_packet_source_mapping.py `
  cloud_edge_project/sender_module/tests/test_target_edge_mqtt_routing.py -q
git diff --check
```

Commit boundary:

```text
refactor: use scenario-local bearing ingestion
```

## Task 7: Run focused and full acceptance

Actions:

1. Run all new layout compatibility and architecture tests.
2. Run all existing sender, bearing scenario, compatibility, registry, edge,
   cloud, scheduler, and model-lifecycle tests affected by import resolution.
3. Run the complete repository suite with the formal model environment.
4. Require at least 781 baseline tests plus the new tests.
5. Run `git diff --check`, inspect every file in the Stage 10 commit range,
   and confirm the protected worktree entries are unchanged.
6. Confirm there is one implementation owner and all three old imports work in
   a clean Python process.

Acceptance:

- all focused and full tests pass;
- no packet byte, error, database, controller, MQTT, scheduler, H5, or model
  behavior changes;
- only first-batch production, test, and documentation files are committed.

## Task 8: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved design, this plan, and the
   complete Stage 10 first-batch commit range.
2. Fix every Critical and Important finding with surgical changes.
3. Rerun affected focused tests and the full suite after any fix.
4. Commit review fixes separately when needed.
5. Report the final commit list, focused/full test counts, review result,
   protected worktree status, and overall project progress.
6. Stop after this ingestion batch; do not automatically begin H5, cloud
   diagnosis, storage SQL, training, or global-analysis physical moves.

Acceptance:

- reviewer reports no remaining Critical or Important finding;
- every test and architecture guard passes;
- the first Stage 10 batch is independently releasable.

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
