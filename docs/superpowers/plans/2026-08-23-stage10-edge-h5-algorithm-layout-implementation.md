# Stage 10 Edge H5 Algorithm Physical Layout Implementation Plan

## Objective

Move only the bearing H5 algorithm layer from `edge_diagnosis` into
`scenarios.bearing.edge_inference.h5`, preserving every existing model,
feature, tensor, output, error, artifact, and import behavior through the
bearing V1.2 compatibility boundary.

## Success Criteria

- Feature extraction, the H5 network, and the distilled model runner have one
  implementation owner under `scenarios.bearing.edge_inference.h5`.
- Existing `edge_diagnosis` imports remain valid through
  `compatibility.bearing_v12.edge_h5_exports`.
- Retained public classes, functions, exceptions, and constants have the
  approved identity/value relationship.
- Fixed-probe tensors, probabilities, class, confidence, result, risk,
  evidence, versions, and errors remain equivalent.
- H5 client, probe loader, lifecycle, weights, manifests, resources, Docker,
  and Git LFS content do not change.
- The full suite passes with at least the 811-test baseline plus new tests.
- Independent review reports no remaining Critical or Important finding.

## Guardrails

- Complete and report each task before starting the next one.
- Move files in this order: features, network, distilled runner.
- Do not change algorithm statements while moving them, except import paths
  and explicit exports required by the approved physical layout.
- Do not move or edit `edge_model/local_h5_client.py` or
  `edge_model/h5_probe.py`.
- Do not regenerate, alter, or recommit the fixed probe.
- Do not move or modify model weights, manifests, normalization files, model
  directories, probe resources, Dockerfile, or Git LFS configuration.
- Do not modify provider, service, scheduler, cloud, or lifecycle behavior.
- Do not touch the protected user-owned deleted or untracked files.
- Stop if output equivalence fails or a broader logic change becomes
  necessary.

## Task 1: Freeze the H5 algorithm baseline

Actions:

1. Record branch, HEAD, and complete short worktree status.
2. Confirm the protected worktree entries are unchanged.
3. Record the tracked paths and hashes for:
   - the three current algorithm files;
   - the fixed probe manifest and NPZ;
   - the bundled model manifest, checkpoint, and normalization files;
   - `freeze_h5_probe.py`, Dockerfile, and Git LFS configuration.
4. Record the current public names and complete import graph for the three
   algorithm modules.
5. Run the existing H5 diagnosis, local route, official route, update
   lifecycle, worker, and provider tests.
6. Execute the committed fixed probe through the existing runtime tests and
   record its tensor/output/version evidence.
7. Run the complete suite with the formal environment and confirm 811 tests
   pass.

The repository contains the committed canonical probe but not its original
MAT source. Therefore `freeze_h5_probe.py` is not used to regenerate the
artifact. Its hash is frozen and the committed probe is executed read-only.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/edge_service/verification/test_distilled_h5_diagnosis.py `
  cloud_edge_project/edge_service/verification/test_local_h5_route.py `
  cloud_edge_project/edge_service/verification/test_official_model_route.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/edge_service/verification/test_worker_guard.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_inference_provider.py -q
```

No file changes are allowed in this task.

## Task 2: Add failing layout and equivalence contracts

Files:

- add
  `cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py`;
- modify
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Define the exact retained public API for features, network, and runner.
2. Add old/compatibility/scenario object-identity contracts.
3. Add architecture rules requiring old modules to use only the compatibility
   boundary and scenario modules to avoid old H5 shims.
4. Add single-owner and thin-shim AST rules, with no wildcard imports.
5. Add independent fixed feature-vector golden assertions.
6. Add network structure/state-dict and deterministic forward-output goldens
   using a fixed seed and evaluation mode.
7. Add fixed model input-tensor, result, probability, evidence, and exact error
   goldens using the existing bundled artifacts and committed probe.
8. Add cold-import and legacy-pickle contracts.
9. Run the new contracts and record that only the not-yet-created layout and
   dependency direction fail.

Acceptance:

- existing H5 behavior tests remain green;
- new failures identify only missing scenario modules, compatibility exports,
  or old implementation ownership;
- no production file changes occur.

Commit boundary:

```text
test: define edge h5 layout contracts
```

## Task 3: Move H5 feature extraction

Files:

- add `cloud_edge_project/scenarios/bearing/edge_inference/h5/__init__.py`;
- add `cloud_edge_project/scenarios/bearing/edge_inference/h5/features.py`;
- add `cloud_edge_project/compatibility/bearing_v12/edge_h5_exports.py`;
- replace `cloud_edge_project/edge_service/src/edge_diagnosis/h5_features.py`
  with an explicit thin shim;
- extend the focused compatibility tests.

Actions:

1. Move `_compute_single`, `normalize_features`, and their existing feature
   calculations without logic edits.
2. Add explicit compatibility exports and a matching legacy `__all__`.
3. Compare exact 19-element feature vectors for fixed valid inputs.
4. Compare normalization output, dtype, shape, and covered error behavior.
5. Assert old/new function identity.
6. Run existing diagnosis tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py `
  cloud_edge_project/edge_service/verification/test_distilled_h5_diagnosis.py -q `
  -k "features"
git diff --check
```

Commit boundary:

```text
refactor: move bearing h5 feature implementation
```

## Task 4: Move the H5 network definition

Files:

- add `cloud_edge_project/scenarios/bearing/edge_inference/h5/network.py`;
- extend `cloud_edge_project/compatibility/bearing_v12/edge_h5_exports.py`;
- replace `cloud_edge_project/edge_service/src/edge_diagnosis/h5_network.py`
  with an explicit thin shim;
- extend the focused compatibility tests.

Actions:

1. Move `PhysicalFusionModel` without changing any layer, dimension, dropout,
   condition scaling, constructor default, or forward calculation.
2. Add explicit compatibility exports and a matching legacy `__all__`.
3. Assert old/new class identity.
4. Compare constructor attributes and every state-dict key/shape.
5. Use a fixed seed, evaluation mode, and fixed tensors to compare exact
   forward shapes and deterministic outputs.
6. Run existing diagnosis and checkpoint-loading tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py `
  cloud_edge_project/edge_service/verification/test_distilled_h5_diagnosis.py -q `
  -k "network or runtime_dependencies"
git diff --check
```

Commit boundary:

```text
refactor: move bearing h5 network implementation
```

## Task 5: Move the distilled H5 model runner

Files:

- add
  `cloud_edge_project/scenarios/bearing/edge_inference/h5/distilled_h5_model.py`;
- modify `cloud_edge_project/scenarios/bearing/edge_inference/h5/__init__.py`;
- extend `cloud_edge_project/compatibility/bearing_v12/edge_h5_exports.py`;
- replace
  `cloud_edge_project/edge_service/src/edge_diagnosis/distilled_h5_model.py`
  with an explicit thin shim;
- modify `cloud_edge_project/edge_service/src/edge_diagnosis/__init__.py` only
  if necessary to retain its exact existing package exports;
- extend the focused compatibility tests.

Actions:

1. Move the complete runner and private helpers without logic edits.
2. Change only its feature/network imports to scenario-local modules.
3. Export `H5_LABELS`, `RUNTIME_MODEL_VERSION`, `H5ModelArtifactError`, and
   `DistilledH5DiagnosticModel` explicitly through compatibility and the old
   shim.
4. Preserve the package-level `H5_RUNTIME_MODEL_VERSION` alias.
5. Assert old/new class, exception, and constant compatibility.
6. Compare fixed probe tensors, probabilities, label, confidence, result,
   risk, evidence, versions, and exact errors.
7. Verify cancellation checkpoints and artifact validation behavior.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py `
  cloud_edge_project/edge_service/verification/test_distilled_h5_diagnosis.py `
  cloud_edge_project/edge_service/verification/test_local_h5_route.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing distilled h5 runner
```

## Task 6: Complete compatibility and integration acceptance

Files:

- finalize
  `cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py`;
- finalize
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Import each old module first in an isolated process and verify all retained
   exports resolve.
2. Repeat with scenario modules first and compatibility module first.
3. Resolve legacy pickle globals for the model and network classes without
   pre-importing scenario modules.
4. Verify `LocalH5ModelClient` still loads through the old model path and
   returns the scenario-owned class.
5. Verify the three old modules contain only docstrings, explicit
   compatibility imports, and `__all__`.
6. Verify only scenario files own feature/network/runner definitions.
7. Verify H5 client, probe, model store, version store, poller, worker,
   provider, artifacts, resources, Dockerfile, and LFS files have no diff.
8. Run model activation, rollback, readiness, worker, and provider tests.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/edge_service/verification/test_official_model_route.py `
  cloud_edge_project/edge_service/verification/test_worker_guard.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_inference_provider.py -q
git diff --check
```

Commit boundary:

```text
test: verify scenario-owned edge h5 algorithms
```

## Task 7: Run focused and full acceptance

Actions:

1. Run all new layout, equivalence, import, pickle, and architecture tests.
2. Run every existing H5 diagnosis, route, update-lifecycle, worker, provider,
   compatibility, edge runtime, and model-lifecycle test.
3. Execute the committed fixed probe through the unchanged model/runtime path.
4. Compare the final probe resource and all protected H5 file hashes with
   Task 1.
5. Run the complete repository suite with the formal model environment.
6. Require at least 811 baseline tests plus new tests.
7. Run `git diff --check` and inspect every file in the batch commit range.
8. Confirm the protected user worktree remains unchanged.

Acceptance:

- all focused and full tests pass;
- all frozen hashes outside the approved source/test files are unchanged;
- no tensor, probability, result, error, artifact, lifecycle, route, or weak
  network behavior changes;
- only this H5 algorithm batch's source, compatibility, tests, and documents
  are committed.

## Task 8: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved design, this plan, and the
   complete H5 algorithm commit range.
2. Require explicit review of semantic-copy fidelity, import cycles, legacy
   imports/pickles, model structure, golden evidence, artifact hashes, and
   commit confinement.
3. Fix every Critical and Important finding with surgical changes.
4. Rerun affected focused tests and the full suite after any fix.
5. Commit review fixes separately when needed.
6. Report the final commit list, test counts, probe/artifact hash result,
   review verdict, and protected worktree status.
7. Stop after this algorithm batch; do not automatically start the H5
   client/probe physical move.

Acceptance:

- reviewer reports no remaining Critical or Important finding;
- all tests and architecture guards pass;
- the H5 algorithm batch is independently releasable.

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
