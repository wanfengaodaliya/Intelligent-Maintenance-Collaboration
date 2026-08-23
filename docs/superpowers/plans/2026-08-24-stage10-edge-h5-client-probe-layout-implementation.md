# Stage 10 Edge H5 Client and Probe Physical Layout Implementation Plan

## Objective

Move the concrete bearing H5 runtime client and activation probe loader from
the generic edge package into `scenarios.bearing.edge_inference`. Preserve the
platform-owned model store, version pointers, download, polling, reporting,
rollback, resources, artifacts, legacy imports, and every observable runtime
behavior through explicit compatibility shims and a scenario-neutral model
activation error contract.

## Success Criteria

- `LocalH5ModelClient` and the H5 probe loader each have one production
  implementation owner under `scenarios.bearing.edge_inference`.
- `BearingEdgeInferenceProvider` directly assembles the scenario-owned client.
- The generic model-update poller imports only a scenario-neutral activation
  error and no H5 implementation.
- Existing `edge_model.local_h5_client` and `edge_model.h5_probe` imports,
  public objects, pickle globals, and cold-import orders remain compatible.
- Probe resources, manifests, NPZ bytes, model weights, normalization files,
  active pointers, Git LFS content, and Docker layout remain unchanged.
- Readiness, inference, evidence, activation, rollback, error messages, stable
  error codes, locking, fallback, and persistence remain equivalent.
- The full suite passes with at least the accepted 902-test baseline plus new
  contracts.
- Independent review reports no remaining Critical or Important issue.

## Guardrails

- Complete, commit, and report each task before starting the next task.
- Add failing layout/behavior contracts before moving production code.
- Move the probe before the client so each implementation owner changes once.
- Do not edit the accepted H5 algorithm modules except import-only corrections
  that are proven necessary; stop before making any algorithm change.
- Do not rewrite model-store, version-store, pull, poller, activation, rollback,
  or pointer algorithms.
- Do not move or edit probe resources, model assets, manifests, normalization,
  Docker statements, or Git LFS configuration.
- Do not modify cloud model update, offline training, storage, scheduler,
  global analysis, or cloud diagnosis.
- Do not touch protected user-owned deleted or untracked files.
- Stop if output or error equivalence fails or another responsibility group is
  required.

## Task 1: Freeze the client and probe baseline

Actions:

1. Record branch, HEAD, short worktree status, and protected entries.
2. Record Git blob IDs and SHA-256 hashes for:
   - `edge_model/local_h5_client.py`;
   - `edge_model/h5_probe.py`;
   - probe `manifest.json` and `probe.npz`;
   - model-store, version-store, model-pull, poller, Provider, Dockerfile, and
     Git LFS configuration.
3. Record the public names, object modules, import graph, constructor
   signatures, dataclass defaults, and exception hierarchy.
4. Freeze normalized outputs for:
   - default probe path and parsed manifest;
   - reconstructed probe task and raw packet;
   - all covered probe validation failures;
   - client readiness, inference success/failure, evidence, current version,
     activation success/failure, rollback, and pointer recovery;
   - poller success/failure summaries and stable error codes.
5. Run the existing fixed-probe, H5 route, lifecycle, worker, Provider,
   architecture, weak-network, and model-update neighbor tests.
6. Run the complete suite and confirm the accepted 902-test baseline.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/edge_service/verification/test_distilled_h5_diagnosis.py `
  cloud_edge_project/edge_service/verification/test_local_h5_route.py `
  cloud_edge_project/edge_service/verification/test_official_model_route.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/edge_service/verification/test_worker_guard.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_inference_provider.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py -q
```

No file changes are allowed in this task.

## Task 2: Add failing client/probe layout and behavior contracts

Files:

- add
  `cloud_edge_project/tests/scenarios/bearing/test_edge_h5_runtime_layout_compatibility.py`;
- modify
  `cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py`.

Actions:

1. Define exact retained public exports for the old client and probe modules.
2. Add old/compatibility/scenario object-identity contracts.
3. Add single-owner tests for the client, config, H5 activation error, probe
   error, manifest reader, and probe loader.
4. Add thin-shim AST tests requiring explicit imports and `__all__` only.
5. Add cold-import and legacy-pickle contracts for both old modules.
6. Add a generic-poller dependency test prohibiting H5, bearing, and
   compatibility imports.
7. Add a Provider assembly test requiring direct scenario runtime use.
8. Freeze the exact default probe path and verify the committed resource hashes
   remain unchanged.
9. Add independent goldens for client/probe outputs and errors not already
   covered by existing tests.
10. Run the new tests and record that failures are limited to missing target
    modules, missing generic activation error, old implementation ownership,
    and old Provider/poller wiring.

Acceptance:

- all existing H5 tests remain green;
- new failures describe only the planned migration boundary;
- no production file changes occur.

Commit boundary:

```text
test: define h5 client probe layout contracts
```

## Task 3: Add the scenario-neutral activation error contract

Files:

- modify `cloud_edge_project/core/model_lifecycle.py`;
- extend focused contract tests.

Actions:

1. Add the minimal `ModelActivationError` runtime error under the generic model
   lifecycle contract.
2. Keep the class free of H5, bearing, model-family, probe, and storage terms.
3. Do not change any current exception producer or consumer in this task.
4. Verify the new contract imports cold and does not load scenario modules.
5. Run core lifecycle, Provider, poller, and architecture neighbors.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/contracts/test_model_lifecycle.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py -q
git diff --check
```

Commit boundary:

```text
refactor: add generic model activation error
```

## Task 4: Move the H5 probe loader

Files:

- add `cloud_edge_project/scenarios/bearing/edge_inference/h5_probe.py`;
- add
  `cloud_edge_project/compatibility/bearing_v12/edge_h5_runtime_exports.py`;
- replace `cloud_edge_project/edge_service/src/edge_model/h5_probe.py` with an
  explicit compatibility shim;
- extend focused tests.

Actions:

1. Move the complete probe implementation without changing validation order,
   messages, channel contracts, identities, hashes, task construction, or
   output.
2. Change only the `PacketInferenceTask` import to the existing platform
   contract path required by the new package location.
3. Recalculate `default_probe_dir()` from the scenario module to the unchanged
   sibling `edge_service/resources/model_probes/distilled_h5/v1` directory.
4. Export all retained public names explicitly through compatibility and the
   old path.
5. Assert old/new object identity, cold imports, pickle globals, source and
   container-layout path resolution, and exact errors.
6. Compare manifest and NPZ hashes with Task 1.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_runtime_layout_compatibility.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_layout_compatibility.py -q `
  -k "probe"
git diff --check
```

Commit boundary:

```text
refactor: move bearing h5 probe loader
```

## Task 5: Move the concrete H5 runtime client

Files:

- add
  `cloud_edge_project/scenarios/bearing/edge_inference/local_h5_client.py`;
- extend
  `cloud_edge_project/compatibility/bearing_v12/edge_h5_runtime_exports.py`;
- replace
  `cloud_edge_project/edge_service/src/edge_model/local_h5_client.py` with an
  explicit compatibility shim;
- extend focused tests.

Actions:

1. Move the concrete client, config, handle, constants, locks, inference,
   activation, pointer recovery, and validation logic without algorithm edits.
2. Change imports only to:
   - the scenario-owned probe and accepted H5 diagnostic runner;
   - generic edge contracts, model-client result objects, and version-store
     helpers;
   - `core.model_lifecycle.ModelActivationError`.
3. Make `H5ActivationError` subclass `ModelActivationError` while preserving
   all direct client errors as `H5ActivationError` with exact text.
4. Export retained names explicitly through compatibility and the old path.
5. Assert old/new class and exception identity, constructor/default equality,
   cold imports, and pickle globals.
6. Compare readiness, inference, evidence, activation, rollback, pointer, lock,
   and exact failure behavior with Task 1.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_runtime_layout_compatibility.py `
  cloud_edge_project/edge_service/verification/test_local_h5_route.py `
  cloud_edge_project/edge_service/verification/test_official_model_route.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/edge_service/verification/test_worker_guard.py -q
git diff --check
```

Commit boundary:

```text
refactor: move bearing h5 runtime client
```

## Task 6: Switch Provider assembly and genericize poller errors

Files:

- modify
  `cloud_edge_project/scenarios/bearing/edge_inference/provider.py`;
- modify
  `cloud_edge_project/edge_service/src/edge_runtime/model_update_poller.py`;
- finalize focused and architecture tests.

Actions:

1. Have the Provider import and construct the scenario-owned client directly.
   Keep that import lazy inside the metadata/client assembly methods so
   building non-edge registries does not require the edge runtime package.
2. Keep generic model-store initialization and selection unchanged.
3. Remove the Provider's fallback imports through the old client path.
4. Have the poller import and catch `ModelActivationError` instead of
   `H5ActivationError`.
5. Raise `ModelActivationError` for the poller's own generic activation and
   rollback confirmation failures with the exact existing messages.
6. Preserve handling order for model-pull, activation, key, and value errors.
7. Verify application startup still obtains the runtime only through the
   registry and Provider.
8. Verify no generic edge runtime file imports the scenario, compatibility
   package, or an H5 error.

Verification:

```powershell
python -m pytest `
  cloud_edge_project/tests/scenarios/bearing/test_edge_h5_runtime_layout_compatibility.py `
  cloud_edge_project/tests/scenarios/bearing/test_edge_inference_provider.py `
  cloud_edge_project/tests/architecture/test_scenario_dependency_rules.py `
  cloud_edge_project/edge_service/verification/test_h5_model_update_lifecycle.py `
  cloud_edge_project/edge_service/verification/test_local_h5_route.py -q
git diff --check
```

Commit boundary:

```text
refactor: inject scenario h5 runtime client
```

## Task 7: Run focused and full acceptance

Actions:

1. Run all new layout, identity, thin-shim, owner, cold-import, pickle,
   resource-path, Provider, and poller dependency tests.
2. Run all existing H5 diagnosis, route, lifecycle, worker, weak-network,
   Provider, model-pull, architecture, and Docker-import neighbors.
3. Compare normalized client/probe goldens with Task 1.
4. Confirm probe manifest, NPZ, model assets, normalization, Dockerfile, Git
   LFS, model-store, version-store, and model-pull hashes are unchanged.
5. Run the complete suite and require at least 902 baseline tests plus new
   contracts.
6. Run `git diff --check` over the complete batch range and inspect every
   changed file.
7. Confirm protected user worktree entries remain unchanged and uncommitted.

Acceptance:

- all focused and full tests pass;
- client, probe, lifecycle, resource, output, error, and compatibility behavior
  are equivalent;
- only this batch's code, compatibility, tests, and documents are committed.

No commit is required unless acceptance exposes a test-only gap.

## Task 8: Independent review and final acceptance

Actions:

1. Request a read-only review against the approved design, this plan, and the
   complete batch commit range.
2. Require explicit review of implementation ownership, compatibility shims,
   import direction, probe resource path, exception hierarchy, locking,
   activation atomicity, pointer recovery, Provider assembly, poller behavior,
   artifacts, and commit confinement.
3. Fix every Critical and Important finding with surgical changes.
4. Rerun affected focused tests and the full suite after every fix.
5. Commit review fixes separately when required.
6. Report final commits, test counts, hash/golden results, review verdict,
   protected worktree status, and remaining Stage 10 batches.
7. Stop after this H5 client/probe batch; do not automatically start offline
   training/model-update or storage physical movement.

Acceptance:

- reviewer reports no remaining Critical or Important issue;
- all tests and architecture guards pass;
- the H5 client/probe batch is independently releasable.

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
