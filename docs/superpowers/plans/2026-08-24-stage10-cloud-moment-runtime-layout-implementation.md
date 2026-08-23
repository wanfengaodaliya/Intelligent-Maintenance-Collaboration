# Stage 10 Cloud MOMENT Runtime Physical Layout Implementation Plan

## Objective

Move only the bearing cloud MOMENT diagnosis runtime from `cloud_service` into
`scenarios.bearing.cloud_diagnosis`, preserving every existing model-loading,
condition-transform, prediction, policy, lifecycle, error, path, and import
behavior through the bearing V1.2 compatibility boundary.

## Success Criteria

- The MOMENT backbone loader and LIGHT_ADAPT runtime have one implementation
  owner under `scenarios.bearing.cloud_diagnosis`.
- Existing `cloud_service.moment_backbone` and
  `cloud_service.moment_light_adapt` imports remain valid through
  `compatibility.bearing_v12.cloud_moment_exports`.
- Retained public functions, classes, dataclasses, and constants have the
  approved identity/value relationship.
- Fixed condition vectors, model-loader calls, predictions, probabilities,
  labels, confidence, decisions, paths, and errors remain equivalent.
- `cloud_service.service`, offline trainers, model assets, activation,
  rollback, APIs, global analysis, and storage do not change.
- The full suite passes with at least the accepted 855-test baseline plus new
  tests.
- Independent review reports no remaining Critical or Important issue.

## Guardrails

- Complete and report each task before starting the next one.
- Move files in this order: backbone loader, then LIGHT_ADAPT runtime.
- Do not change algorithm or policy statements while moving them, except the
  explicit project-root fallback adjustment required by the new file depth.
- Do not modify `cloud_service.service` or its runner cache, locking,
  activation, rollback, or inference orchestration.
- Do not modify `cloud_service.model_update.training`; it must continue using
  the legacy backbone import unchanged.
- Do not move or modify checkpoints, pretrained model files, normalization
  files, deployment sources, manifests, or Git LFS configuration.
- Do not move offline training, global-analysis, or storage responsibilities.
- Do not use wildcard imports or duplicate implementation.
- Do not touch protected user-owned deleted or untracked files.
- Stop if old/new behavior differs or a broader logic change is required.

## Task 1: Freeze the cloud MOMENT runtime baseline

Actions:

1. Record branch, HEAD, and complete short worktree status.
2. Confirm all protected user-owned entries are unchanged.
3. Record tracked paths and SHA-256 hashes for:
   - `cloud_service/moment_backbone.py`;
   - `cloud_service/moment_light_adapt.py`;
   - configured MOMENT checkpoint;
   - configured condition normalization file;
   - deployed `moment_model.py`;
   - relevant Git LFS configuration.
4. Record the public names and complete import graph for both implementation
   modules.
5. Capture fixed golden evidence for:
   - backbone-loader calls with an injected fake pipeline;
   - condition-vector values, shape, order, and dtype;
   - review-policy mappings and errors;
   - workspace-root discovery and fallback;
   - unloaded runner, device resolution, and GPU status;
   - deterministic fake-model prediction output.
6. Run the existing cloud runtime, V1.2 inference, model lifecycle, training,
   provider, registry, portability, and architecture tests.
7. Run the complete suite with the formal environment and confirm 855 tests
   pass.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/cloud_service/tests/test_cloud_status_runtime.py `
  cloud_edge_project/cloud_service/tests/test_moment_final_model_config.py `
  cloud_edge_project/cloud_service/tests/test_moment_runtime_lifecycle.py `
  cloud_edge_project/cloud_service/tests/test_v12_cloud_infer_contract.py `
  cloud_edge_project/cloud_service/tests/test_model_update_training.py `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_capability_providers.py `
  cloud_edge_project/cloud_service/tests/test_scenario_registry_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_portability.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py -q
```

No file changes are allowed in this task.

## Task 2: Add failing physical-layout and equivalence contracts

Files:

- add
  `cloud_edge_project/tests/scenarios/bearing/test_cloud_moment_layout_compatibility.py`;
- modify
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Define the exact retained public API for the backbone and LIGHT_ADAPT
   modules.
2. Add old/compatibility/scenario object-identity contracts.
3. Add source rules requiring legacy cloud modules to import only the
   compatibility boundary, not `scenarios.bearing`.
4. Add rules requiring scenario implementation modules to avoid both legacy
   MOMENT shims.
5. Add single-owner and thin-shim AST rules; allow only a string module
   docstring, explicit `ImportFrom` statements, and `__all__` assignment.
6. Add independent goldens for condition vectors, policy mappings, workspace
   resolution, backbone calls, runner state/device behavior, and deterministic
   fake prediction output.
7. Add cold-import and legacy-pickle resolution contracts for retained public
   classes.
8. Run the new tests and record that only the missing scenario layout,
   compatibility exports, and old implementation ownership fail.

Acceptance:

- existing MOMENT behavior tests remain green;
- failures point only to the not-yet-created target modules or old dependency
  direction;
- no production file changes occur.

Commit boundary:

```text
test: define cloud moment layout contracts
```

## Task 3: Move the MOMENT backbone loader

Files:

- add
  `cloud_edge_project/scenarios/bearing/cloud_diagnosis/moment_backbone.py`;
- add
  `cloud_edge_project/compatibility/bearing_v12/cloud_moment_exports.py`;
- replace `cloud_edge_project/cloud_service/moment_backbone.py` with an
  explicit thin shim;
- extend the focused layout compatibility tests.

Actions:

1. Move `load_moment_backbone` without logic edits.
2. Export it explicitly from `cloud_moment_exports` and from the legacy shim.
3. Define exact matching `__all__` lists.
4. Assert old/new function identity.
5. Capture fake pipeline calls and compare pretrained path, task name, channel
   count, class count, initialization, warning behavior, result identity, and
   propagated errors.
6. Verify `cloud_service.model_update.training` still imports and uses the old
   backbone path without modification.
7. Run the existing model-update training tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_moment_layout_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_model_update_training.py -q `
  -k "backbone or cloud"
git diff --check
```

Commit boundary:

```text
refactor: move bearing moment backbone loader
```

## Task 4: Move the MOMENT LIGHT_ADAPT runtime

Files:

- add
  `cloud_edge_project/scenarios/bearing/cloud_diagnosis/moment_light_adapt.py`;
- modify
  `cloud_edge_project/scenarios/bearing/cloud_diagnosis/__init__.py` only if
  explicit scenario-local exports are required;
- extend
  `cloud_edge_project/compatibility/bearing_v12/cloud_moment_exports.py`;
- replace `cloud_edge_project/cloud_service/moment_light_adapt.py` with an
  explicit thin shim;
- extend the focused layout compatibility tests.

Actions:

1. Move `LABEL_NAMES`, `MODEL_VERSION`, `MomentPrediction`,
   `MomentReviewPolicy`, `build_condition_vector`,
   `deployment_workspace_root`, and `MomentLightAdaptRunner` without behavior
   edits.
2. Change the backbone import to the scenario-local module.
3. Replace the file-depth-dependent fallback with an explicit expression that
   returns the same `cloud_edge_project` root as before migration.
4. Export every retained public name explicitly through compatibility and the
   old shim with matching `__all__`.
5. Assert old/new class, dataclass, function, and constant compatibility.
6. Compare exact condition vectors, policy results/errors, root discovery,
   fallback, runner properties/errors, fake model-loader behavior, adapter
   registration, device resolution, and deterministic fake predictions.
7. Run existing cloud runtime and V1.2 diagnosis tests before proceeding.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_moment_layout_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_cloud_status_runtime.py `
  cloud_edge_project/cloud_service/tests/test_moment_final_model_config.py `
  cloud_edge_project/cloud_service/tests/test_moment_runtime_lifecycle.py `
  cloud_edge_project/cloud_service/tests/test_v12_cloud_infer_contract.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing cloud moment runtime
```

## Task 5: Complete compatibility and integration acceptance

Files:

- finalize
  `cloud_edge_project/tests/scenarios/bearing/test_cloud_moment_layout_compatibility.py`;
- finalize
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Import each old module first in an isolated process and verify all retained
   exports resolve.
2. Repeat with scenario modules first and compatibility module first.
3. Resolve legacy pickle globals for retained public classes without
   pre-importing scenario modules.
4. Verify `cloud_service.service` receives the scenario-owned runner and policy
   through the unchanged legacy import path.
5. Verify `cloud_service.model_update.training` receives the scenario-owned
   backbone loader through its unchanged legacy import path.
6. Verify legacy modules and compatibility exports satisfy the strict AST
   contract and contain no implementation.
7. Verify only scenario files own backbone, condition, policy, prediction, and
   runner definitions.
8. Verify cloud service, training, model assets, deployment assets, lifecycle,
   API, storage, and global-analysis files have no diff.
9. Run registry/provider, portability, lifecycle, activation, rollback, and
   inference tests.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_moment_layout_compatibility.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/tests/scenarios/bearing/test_cloud_capability_providers.py `
  cloud_edge_project/cloud_service/tests/test_scenario_registry_compatibility.py `
  cloud_edge_project/cloud_service/tests/test_portability.py `
  cloud_edge_project/cloud_service/tests/test_moment_runtime_lifecycle.py `
  cloud_edge_project/cloud_service/tests/test_v12_cloud_infer_contract.py `
  cloud_edge_project/cloud_service/tests/test_model_update_training.py -q
git diff --check
```

Commit boundary:

```text
test: verify scenario-owned cloud moment runtime
```

## Task 6: Run focused and full acceptance

Actions:

1. Run all new layout, equivalence, import, pickle, and architecture tests.
2. Run every existing cloud MOMENT status, configuration, inference,
   lifecycle, model-update training, provider, registry, and portability test.
3. Exercise the configured checkpoint and normalization through the unchanged
   runtime test path when the formal assets are available.
4. Compare all frozen source and asset hashes with Task 1; only approved
   source, compatibility, tests, and documents may differ.
5. Run the complete repository suite with the formal environment.
6. Require at least 855 baseline tests plus new tests.
7. Run `git diff --check` and inspect every file in the batch commit range.
8. Confirm protected user worktree entries remain unchanged and uncommitted.

Acceptance:

- all focused and full tests pass;
- model, normalization, deployment, lifecycle, API, storage, training, and
  global-analysis behavior and assets are unchanged;
- no condition-vector, probability, label, confidence, decision, error,
  import, activation, rollback, or path behavior changes;
- only this cloud MOMENT runtime batch's source, compatibility, tests, and
  documents are committed.

## Task 7: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved design, this plan, and the
   complete cloud MOMENT runtime commit range.
2. Require explicit review of semantic-copy fidelity, file-depth path
   handling, import cycles, legacy imports and pickles, condition order,
   label/decision mappings, prediction math, asset hashes, and commit
   confinement.
3. Fix every Critical and Important finding with surgical changes.
4. Rerun affected focused tests and the full suite after every fix.
5. Commit review fixes separately when needed.
6. Report final commits, test counts, hash results, review verdict, protected
   worktree status, and remaining Stage 10 batches.
7. Stop after this cloud MOMENT runtime batch; do not automatically start
   offline training, global-analysis, or storage physical movement.

Acceptance:

- reviewer reports no remaining Critical or Important issue;
- all tests and architecture guards pass;
- the cloud MOMENT runtime batch is independently releasable.

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
